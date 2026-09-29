"""Shared helpers for the Step-1 kernel tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.config import Settings
from app.container import build_container
from app.kernel.models import CapabilityDescriptor
from app.kernel.registry import CapabilityRegistry
from app.routers import health, tasks
from app.testing.fake_capabilities import (
    FakeAskInputCapability,
    FakeReadCapability,
    FakeWriteCapability,
)
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore


class ValueInputPayload(BaseModel):
    value: str = Field(min_length=1)


class CountingReadCapability:
    descriptor = CapabilityDescriptor(
        name="test.read",
        description="Counting read capability for idempotency assertions.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict) -> ValueInputPayload:
        return ValueInputPayload.model_validate(arguments)

    def execute(self, arguments: ValueInputPayload) -> dict:
        self.calls += 1
        return {"value": arguments.value, "calls": self.calls}


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "database_url": "",
        "redis_url": "",
        "task_retry_backoff_seconds": "0",
        "task_max_steps": 8,
        "task_max_execution_seconds": 300.0,
        "task_max_retries": 3,
        "approval_expires_seconds": 3600.0,
        "default_timezone": "Asia/Shanghai",
        "knowledge_ready_stream": "knowledge:ready",
        "knowledge_consumer_group": "agent-workers",
    }
    base.update(overrides)
    return Settings(**base)


def build_test_container(
    *,
    extra_capabilities=(),
    planner=None,
    policy=None,
    understanding_provider=None,
    **settings_overrides,
):
    settings = make_settings(**settings_overrides)
    registry = CapabilityRegistry(
        [
            FakeReadCapability(),
            FakeWriteCapability(),
            FakeAskInputCapability(),
            *extra_capabilities,
        ]
    )
    store = InMemoryAgentStore()
    todo_store = InMemoryTodoStore()
    publisher = FakeTaskPublisher()
    container = build_container(
        settings=settings,
        store=store,
        todo_store=todo_store,
        publisher=publisher,
        registry=registry,
        planner=planner or InputDrivenFakePlanner(),
        policy=policy,
        understanding_provider=understanding_provider,
    )
    return container, store, publisher, registry


def create_task(
    container,
    *,
    text: str = "hello",
    steps: list[dict[str, Any]] | None = None,
    owner_user_id: str = "user-1",
    client_message_id: str | None = None,
):
    payload: dict[str, Any] = {"text": text}
    if steps:
        payload["steps"] = steps
    return container.task_service.create_task(
        owner_user_id=owner_user_id,
        payload=payload,
        client_message_id=client_message_id,
    )


def make_app(container) -> FastAPI:
    tasks.set_container(container)
    application = FastAPI()
    application.include_router(health.router)
    application.include_router(tasks.router)
    return application


def event_types(store, task_id: str) -> list[str]:
    return [event.event_type for event in store.list_events(task_id)]


def build_step2_container(
    *,
    knowledge=None,
    planner=None,
    understanding_provider=None,
    **settings_overrides,
):
    """Real wiring (deterministic planner, descriptor policy, todo capability)
    over the in-memory storage and a fake Knowledge service.

    The fixture name is kept so the step-2/2.5/3 tests keep pointing at one
    place, but the capability under it is ``todo.create``: a schedule and a
    to-do are the same product object now, so there is no calendar capability
    to wire and nothing is written through Knowledge.
    """

    from app.capabilities.todo import TodoCreateCapability
    from app.planning.deterministic import DeterministicPlanner
    from app.policy.descriptor import DescriptorPolicy
    from app.testing.fake_knowledge import FakeKnowledgeClient

    settings = make_settings(**settings_overrides)
    client = knowledge if knowledge is not None else FakeKnowledgeClient()
    todo_store = InMemoryTodoStore()
    registry = CapabilityRegistry(
        [
            TodoCreateCapability(
                todo_store,
                default_timezone=settings.default_timezone,
            ),
        ]
    )
    store = InMemoryAgentStore()
    publisher = FakeTaskPublisher()
    container = build_container(
        settings=settings,
        store=store,
        todo_store=todo_store,
        publisher=publisher,
        registry=registry,
        planner=planner or DeterministicPlanner(default_timezone=settings.default_timezone),
        policy=DescriptorPolicy(registry),
        knowledge=client,
        understanding_provider=understanding_provider,
    )
    return container, store, publisher, client


def recent_sent_at(hours: float = 2.0) -> str:
    """A collected-message timestamp that keeps relative phrases in the future."""

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace(
        "+00:00", "Z"
    )


def snapshot(**overrides):
    """A Knowledge conversation snapshot in the shape fixed by the step-2 doc."""

    value = {
        "knowledge_item_id": "item-1",
        "source_message_id": "message-1",
        "source_attachment_id": None,
        "content_version": 1,
        "acl_version": 3,
        "platform": "feishu",
        "conversation_ingestion_id": "ingestion-1",
        "conversation_type": "private",
        "conversation_name": "研发组",
        "message_type": "text",
        "sender_display_name": "张三",
        "sent_at": recent_sent_at(),
        "text": "明天晚上八点开个评审会，会议室 A",
        "visibility": "resolved",
        "eligible_owners": [{"owner_user_id": "user-1"}],
        "excluded_members": [],
    }
    value.update(overrides)
    return value


def knowledge_event(**overrides):
    payload = {"knowledge_item_id": "item-1", "resource_type": "knowledge_item", "content_version": 1}
    payload.update(overrides)
    return {
        "event_id": "event-1",
        "event_type": "knowledge.ready",
        "schema_version": 1,
        "occurred_at": "2026-09-25T06:12:31Z",
        "trace_id": "trace-1",
        "organization_id": "",
        "producer": "module-2",
        "payload": payload,
    }
