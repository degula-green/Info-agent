"""Deterministic planner tests: text in, at most one to-do step out."""

from __future__ import annotations

from datetime import datetime, timezone

from app.kernel.models import CapabilityDescriptor, TaskEnvelope
from app.planning.deterministic import (
    DEFAULT_CAPABILITY_NAME,
    DeterministicPlanner,
    capability_idempotency_key,
)

PLANNER = DeterministicPlanner(default_timezone="Asia/Shanghai")


def descriptor(name: str = DEFAULT_CAPABILITY_NAME) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        description="创建待办",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        timeout_seconds=10,
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
            "sender_display_name": "张三",
            "sent_at": "2026-09-25T06:12:30Z",
        },
        created_at=datetime(2026, 9, 25, 6, 12, 31, tzinfo=timezone.utc),
    )


def test_schedule_message_creates_one_todo_step() -> None:
    plan = PLANNER.create_plan(
        envelope("明天晚上八点开个评审会，会议室 A"), [descriptor()], []
    )

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.capability == DEFAULT_CAPABILITY_NAME
    assert step.arguments["title"] == "开个评审会，会议室 A"
    assert step.arguments["due_expression"] == "明天晚上八点"
    assert step.arguments["timezone"] == "Asia/Shanghai"
    assert step.arguments["owner_user_id"] == "user-1"
    assert step.arguments["idempotency_key"] == "knowledge_event:user-1:item-1:3"
    assert step.arguments["source"] == {
        "knowledge_item_id": "item-1",
        "content_version": 3,
        "conversation_type": "group",
        "sender_display_name": "张三",
        "sent_at": "2026-09-25T06:12:30Z",
    }
    assert plan.objective == "创建待办：开个评审会，会议室 A"


def test_a_todo_without_any_time_is_still_a_todo() -> None:
    plan = PLANNER.create_plan(envelope("完成登录模块代码"), [descriptor()], [])

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.arguments["title"] == "完成登录模块代码"
    assert step.arguments["due_expression"] == ""
    assert "due_at" not in step.arguments


def test_a_meeting_without_a_time_is_not_blocked() -> None:
    plan = PLANNER.create_plan(envelope("提醒我跟张三开个会"), [descriptor()], [])

    assert len(plan.steps) == 1
    assert "due_at" not in plan.steps[0].arguments


def test_plain_chat_produces_a_zero_step_plan() -> None:
    plan = PLANNER.create_plan(envelope("你好，在吗", source_type="chat"), [descriptor()], [])

    assert plan.steps == []
    assert plan.objective == "没有需要执行的动作"


def test_keyword_only_message_keeps_an_empty_time_expression() -> None:
    plan = PLANNER.create_plan(envelope("记得开会"), [descriptor()], [])

    assert len(plan.steps) == 1
    assert plan.steps[0].arguments["due_expression"] == ""
    assert plan.steps[0].arguments["title"] == "记得开会"


def test_resolvable_time_is_materialised_so_the_approval_shows_it() -> None:
    plan = PLANNER.create_plan(envelope("明天晚上八点开个评审会"), [descriptor()], [])

    # sent_at 2026-09-25T06:12:30Z is 14:12 +08, so 明天晚上八点 = 09-26 20:00 +08.
    assert plan.steps[0].arguments["due_at"] == "2026-09-26T12:00:00Z"
    assert plan.steps[0].arguments["due_expression"] == "明天晚上八点"


def test_ambiguous_time_stays_unresolved_without_blocking_the_todo() -> None:
    plan = PLANNER.create_plan(envelope("明天八点开会"), [descriptor()], [])

    assert "due_at" not in plan.steps[0].arguments
    assert plan.steps[0].arguments["due_expression"] == "明天八点"


def test_a_coarse_due_hint_is_kept_out_of_the_title() -> None:
    plan = PLANNER.create_plan(envelope("下周交房租"), [descriptor()], [])

    assert plan.steps[0].arguments["title"] == "交房租"
    assert plan.steps[0].arguments["due_expression"] == "下周"
    # A whole-week hint means the deadline is the end of next Sunday.
    assert plan.steps[0].arguments["due_at"] == "2026-10-04T15:59:00Z"


def test_date_only_hint_resolves_to_end_of_that_day() -> None:
    plan = PLANNER.create_plan(envelope("明天完成支付模块"), [descriptor()], [])

    assert plan.steps[0].arguments["title"] == "完成支付模块"
    assert plan.steps[0].arguments["due_expression"] == "明天"
    assert plan.steps[0].arguments["due_at"] == "2026-09-26T15:59:00Z"


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
