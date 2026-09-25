"""Deterministic planner tests: text in, at most one calendar step out."""

from __future__ import annotations

from datetime import datetime, timezone

from app.capabilities.calendar import CAPABILITY_NAME
from app.kernel.models import CapabilityDescriptor, TaskEnvelope
from app.planning.deterministic import DeterministicPlanner, capability_idempotency_key
from tests.support import build_step2_container

PLANNER = DeterministicPlanner(default_timezone="Asia/Shanghai")


def descriptor(name: str = CAPABILITY_NAME) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        description="创建日历事件",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        timeout_seconds=20,
    )


def envelope(
    text: str,
    *,
    source_type: str = "knowledge_event",
    source_ref: dict | None = None,
    owner_user_id: str = "user-1",
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-1",
        source_type=source_type,
        owner_user_id=owner_user_id,
        input={"text": text},
        source_ref=source_ref
        or {
            "knowledge_item_id": "item-1",
            "content_version": 3,
            "conversation_type": "group",
            "sent_at": "2026-09-25T06:12:30Z",
        },
        created_at=datetime(2026, 9, 25, 6, 12, 31, tzinfo=timezone.utc),
    )


def test_schedule_message_creates_one_calendar_step() -> None:
    plan = PLANNER.create_plan(
        envelope("明天晚上八点开个评审会，会议室 A"), [descriptor()], []
    )

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.capability == CAPABILITY_NAME
    assert step.arguments["title"] == "开个评审会，会议室 A"
    assert step.arguments["time_expression"] == "明天晚上八点"
    assert step.arguments["timezone"] == "Asia/Shanghai"
    assert step.arguments["owner_user_id"] == "user-1"
    assert step.arguments["idempotency_key"] == "knowledge_event:user-1:item-1:3"
    assert step.arguments["source"] == {
        "knowledge_item_id": "item-1",
        "content_version": 3,
        "conversation_type": "group",
        "sent_at": "2026-09-25T06:12:30Z",
    }
    assert plan.objective == "创建日程：开个评审会，会议室 A"


def test_plain_chat_produces_a_zero_step_plan() -> None:
    plan = PLANNER.create_plan(envelope("你好，在吗", source_type="chat"), [descriptor()], [])

    assert plan.steps == []
    assert plan.objective == "没有需要执行的动作"


def test_keyword_only_message_keeps_an_empty_time_expression() -> None:
    plan = PLANNER.create_plan(envelope("记得开会"), [descriptor()], [])

    assert len(plan.steps) == 1
    assert plan.steps[0].arguments["time_expression"] == ""
    assert plan.steps[0].arguments["title"] == "记得开会"


def test_missing_capability_means_zero_steps() -> None:
    plan = PLANNER.create_plan(envelope("明天晚上八点开会"), [descriptor("other.tool")], [])

    assert plan.steps == []


def test_chat_idempotency_key_falls_back_to_the_task_id() -> None:
    task = envelope("明天晚上八点开会", source_type="chat", source_ref={})

    assert capability_idempotency_key(task) == "chat:user-1:task-1"
    plan = PLANNER.create_plan(task, [descriptor()], [])
    assert plan.steps[0].arguments["idempotency_key"] == "chat:user-1:task-1"


def test_long_title_is_truncated() -> None:
    text = "明天晚上八点" + "项目评审" * 12
    plan = PLANNER.create_plan(envelope(text), [descriptor()], [])

    assert len(plan.steps[0].arguments["title"]) == 30


def test_zero_step_plan_completes_the_task_without_calling_anything() -> None:
    container, store, _publisher, knowledge = build_step2_container()
    task = container.task_service.create_task(owner_user_id="user-1", payload={"text": "你好呀"})

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert stored.status == "succeeded"
    plan = store.get_plan(stored.current_plan_id)
    assert plan.steps == []
    assert store.list_observations(task.task_id) == []
    assert knowledge.calendar_calls == []
