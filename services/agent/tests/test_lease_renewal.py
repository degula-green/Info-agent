"""The runtime must renew a long Task's lease while it is still executing."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from app.container import build_container
from app.kernel.events import utcnow
from app.kernel.models import CapabilityDescriptor, TaskRecord
from app.kernel.registry import CapabilityRegistry
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from tests.support import make_settings


class ValueInput(BaseModel):
    value: str = Field(min_length=1)


class SlowCapability:
    descriptor = CapabilityDescriptor(
        name="test.slow",
        description="Sleep long enough for the lease heartbeat to fire.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def validate(self, arguments: dict[str, Any]) -> ValueInput:
        return ValueInput.model_validate(arguments)

    def execute(self, arguments: ValueInput) -> dict[str, Any]:
        time.sleep(0.7)
        return {"value": arguments.value}


class RecordingStore(InMemoryAgentStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0

    def renew_lease(self, task_id: str, owner: str, seconds: float) -> bool:
        self.renewals += 1
        return super().renew_lease(task_id, owner, seconds)


def test_runtime_renews_the_lease_while_a_task_is_running() -> None:
    store = RecordingStore()
    container = build_container(
        settings=make_settings(
            task_lease_seconds=0.6,
            task_retry_backoff_seconds="0",
        ),
        store=store,
        todo_store=InMemoryTodoStore(),
        publisher=FakeTaskPublisher(),
        registry=CapabilityRegistry([SlowCapability()]),
        planner=InputDrivenFakePlanner(),
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={
            "text": "slow",
            "steps": [{"capability": "test.slow", "arguments": {"value": "x"}}],
        },
    )

    status = container.execution_service.run_task(task.task_id)

    assert status == "succeeded"
    assert store.renewals >= 1


def test_in_memory_renew_lease_requires_the_owner() -> None:
    store = InMemoryAgentStore()
    task = store.create_task(
        TaskRecord(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            status="received",
            input={},
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )

    assert store.acquire_lease(task.task_id, "worker-a", 60) is True
    assert store.renew_lease(task.task_id, "worker-b", 60) is False
    assert store.renew_lease(task.task_id, "worker-a", 60) is True
