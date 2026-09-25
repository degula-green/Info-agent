"""Shared helpers for the Step-1 kernel tests."""

from __future__ import annotations

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
        "calendar_default_duration_minutes": 60,
        "knowledge_ready_stream": "knowledge:ready",
        "knowledge_consumer_group": "agent-workers",
    }
    base.update(overrides)
    return Settings(**base)


def build_test_container(*, extra_capabilities=(), planner=None, policy=None, **settings_overrides):
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
    publisher = FakeTaskPublisher()
    container = build_container(
        settings=settings,
        store=store,
        publisher=publisher,
        registry=registry,
        planner=planner or InputDrivenFakePlanner(),
        policy=policy,
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


def build_step2_container(*, knowledge=None, **settings_overrides):
    """Real step-2 wiring (deterministic planner, descriptor policy, calendar
    capability) over the in-memory store and a fake Knowledge service."""

    from app.capabilities.calendar import CalendarCreateCapability
    from app.planning.deterministic import DeterministicPlanner
    from app.policy.descriptor import DescriptorPolicy
    from app.testing.fake_knowledge import FakeKnowledgeClient

    settings = make_settings(**settings_overrides)
    client = knowledge if knowledge is not None else FakeKnowledgeClient()
    registry = CapabilityRegistry(
        [
            CalendarCreateCapability(
                client,
                default_timezone=settings.default_timezone,
                default_duration_minutes=settings.calendar_default_duration_minutes,
            )
        ]
    )
    store = InMemoryAgentStore()
    publisher = FakeTaskPublisher()
    container = build_container(
        settings=settings,
        store=store,
        publisher=publisher,
        registry=registry,
        planner=DeterministicPlanner(default_timezone=settings.default_timezone),
        policy=DescriptorPolicy(registry),
        knowledge=client,
    )
    return container, store, publisher, client


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
        "sent_at": "2026-09-25T06:12:30Z",
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
