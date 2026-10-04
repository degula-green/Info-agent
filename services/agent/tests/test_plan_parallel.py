from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.kernel.graph import normalize_plan_dependencies, ready_steps
from app.kernel.models import CapabilityDescriptor, Plan, PlanStep
from app.kernel.models import OutboxEvent
from app.testing.fake_capabilities import FakeValueInput
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from tests.support import build_test_container, create_task


class ConcurrentReadCapability:
    descriptor = CapabilityDescriptor(
        name="test.concurrent_read",
        description="Read concurrently and expose the observed overlap.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def validate(self, arguments: dict) -> FakeValueInput:
        return FakeValueInput.model_validate(arguments)

    def execute(self, arguments: FakeValueInput) -> dict:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return {"operation": "read", "value": arguments.value}


class FastAnswerCapability:
    descriptor = CapabilityDescriptor(
        name="answer.compose",
        description="Return a terminal answer.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def validate(self, arguments: dict) -> FakeValueInput:
        return FakeValueInput.model_validate(arguments)

    def execute(self, arguments: FakeValueInput) -> dict:
        return {"answer": arguments.value, "citations": []}


class CountingLlmPlanner:
    name = "llm"

    def __init__(self) -> None:
        self.decision_calls = 0

    def create_plan(self, task, capabilities, observations, constraints=None, understanding=None):
        del capabilities, observations, constraints, understanding
        plan_id = "llm-plan-1"
        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective="answer",
            steps=[
                PlanStep(
                    step_id=f"{plan_id}-step-1",
                    plan_id=plan_id,
                    order=1,
                    capability="answer.compose",
                    arguments={"value": "ok"},
                )
            ],
        )

    def decide_after_observation(
        self,
        task,
        current_plan,
        observations,
        constraints,
        understanding=None,
    ):
        del task, current_plan, observations, constraints, understanding
        self.decision_calls += 1
        raise AssertionError("terminal answer should not call the model planner")


def _plan(*, explicit: bool) -> Plan:
    dependencies = [] if explicit else None
    return Plan(
        plan_id="plan-1",
        task_id="task-1",
        objective="test",
        steps=[
            PlanStep(
                step_id="step-1",
                plan_id="plan-1",
                order=1,
                capability="test.concurrent_read",
                arguments={"value": "a"},
                depends_on=dependencies,
            ),
            PlanStep(
                step_id="step-2",
                plan_id="plan-1",
                order=2,
                capability="test.concurrent_read",
                arguments={"value": "b"},
                depends_on=dependencies,
            ),
        ],
    )


def test_missing_dependencies_preserve_sequential_contract() -> None:
    normalized = normalize_plan_dependencies(_plan(explicit=False))
    assert normalized.steps[0].depends_on == []
    assert normalized.steps[1].depends_on == ["step-1"]
    assert [step.step_id for step in ready_steps(normalized.steps)] == ["step-1"]


def test_explicit_empty_dependencies_are_ready_together() -> None:
    normalized = normalize_plan_dependencies(_plan(explicit=True))
    assert normalized.steps[0].depends_on == []
    assert normalized.steps[1].depends_on == []
    assert [step.step_id for step in ready_steps(normalized.steps)] == [
        "step-1",
        "step-2",
    ]


def test_parallel_read_batch_runs_more_than_one_step() -> None:
    capability = ConcurrentReadCapability()
    container, store, _, _ = build_test_container(
        extra_capabilities=[capability],
        planner=InputDrivenFakePlanner(),
        task_parallel_enabled=True,
        task_step_concurrency=2,
    )
    task = create_task(
        container,
        steps=[
            {
                "capability": capability.descriptor.name,
                "arguments": {"value": "a"},
                "depends_on": [],
            },
            {
                "capability": capability.descriptor.name,
                "arguments": {"value": "b"},
                "depends_on": [],
            },
        ],
    )

    result = container.execution_service.run_task(task.task_id)

    assert result == "succeeded"
    assert capability.max_active == 2
    assert len(store.list_observations(task.task_id)) == 2


def test_outbox_claim_excludes_sibling_dispatchers_until_released() -> None:
    store = InMemoryAgentStore()
    event = OutboxEvent(
        event_id=str(uuid4()),
        task_id="task-1",
        event_type="agent.task.wakeup",
        payload={},
        created_at=datetime.now(timezone.utc),
    )
    store.enqueue_outbox(event)

    claimed = store.claim_outbox(limit=10, lease_seconds=60)
    assert [item.event_id for item in claimed] == [event.event_id]
    assert store.claim_outbox(limit=10, lease_seconds=60) == []

    store.mark_outbox_failed(event.event_id, "test failure")
    assert store.outbox[event.event_id].status == "pending"


def test_terminal_answer_skips_the_closing_model_decision() -> None:
    planner = CountingLlmPlanner()
    container, _, _, _ = build_test_container(
        extra_capabilities=[FastAnswerCapability()],
        planner=planner,
    )
    task = create_task(container, text="answer this")

    result = container.execution_service.run_task(task.task_id)

    assert result == "succeeded"
    assert planner.decision_calls == 0
