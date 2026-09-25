"""Stage 2a: the Task → Plan → Approval → Execute → Observation loop."""

from __future__ import annotations

import pytest

from app.infrastructure.knowledge.client import CalendarNotBound, CalendarResultUnknown
from app.testing.fake_knowledge import FakeKnowledgeClient
from tests.support import build_step2_container, event_types, knowledge_event, snapshot


def drive_to_approval(container, store, task_id: str):
    assert container.execution_service.run_task(task_id) == "waiting_approval"
    pending = [
        item
        for item in store.list_approvals(task_id=task_id)
        if item.status == "waiting_approval"
    ]
    assert pending, "no pending approval after run_task"
    return pending[-1]


def approve(container, store, approval, arguments=None):
    return container.execution_service.approval_gateway.decide(
        approval.approval_id,
        owner_user_id=store.get_task(approval.task_id).owner_user_id,
        approve=True,
        arguments=arguments,
    )


def test_chat_request_creates_one_calendar_event() -> None:
    container, store, _publisher, knowledge = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )

    approval = drive_to_approval(container, store, task.task_id)
    # Nothing is written before the user confirms.
    assert knowledge.calendar_calls == []
    assert approval.capability == "calendar.create"
    assert approval.arguments["title"] == "开个评审会"
    assert approval.arguments["time_expression"] == "明天晚上八点"

    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    assert len(knowledge.calendar_calls) == 1
    call = knowledge.calendar_calls[0]
    assert call["request_id"] == f"chat:user-1:{task.task_id}"
    assert call["owner_user_id"] == "user-1"
    assert call["title"] == "开个评审会"

    events = event_types(store, task.task_id)
    assert events[0] == "task.accepted"
    assert events[-1] == "task.completed"
    assert events.index("task.waiting_approval") < events.index("step.started")
    assert len(store.list_observations(task.task_id)) == 1


def test_plain_chat_completes_without_any_capability_call() -> None:
    container, store, _publisher, knowledge = build_step2_container()
    task = container.task_service.create_task(owner_user_id="user-1", payload={"text": "晚上好"})

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    assert knowledge.calendar_calls == []
    assert store.list_observations(task.task_id) == []
    assert "task.waiting_approval" not in event_types(store, task.task_id)


def test_collected_message_fans_out_and_each_owner_gets_its_own_event() -> None:
    client = FakeKnowledgeClient(
        snapshots={
            "item-1": snapshot(
                conversation_type="group",
                eligible_owners=[{"owner_user_id": "user-1"}, {"owner_user_id": "user-2"}],
            )
        }
    )
    container, store, _publisher, _client = build_step2_container(knowledge=client)

    outcome = container.knowledge_events.handle(knowledge_event())
    assert len(outcome.task_ids) == 2

    for task_id in outcome.task_ids:
        approval = drive_to_approval(container, store, task_id)
        approve(container, store, approval)
        assert container.execution_service.run_task(task_id) == "succeeded"

    assert len(client.calendar_calls) == 2
    assert {call["owner_user_id"] for call in client.calendar_calls} == {"user-1", "user-2"}
    assert {call["request_id"] for call in client.calendar_calls} == {
        "knowledge_event:user-1:item-1:1",
        "knowledge_event:user-2:item-1:1",
    }


def test_rejecting_the_approval_fails_the_task_without_writing() -> None:
    container, store, _publisher, knowledge = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)

    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=False
    )
    assert container.execution_service.run_task(task.task_id) == "failed"

    assert knowledge.calendar_calls == []
    assert store.get_task(task.task_id).last_error["classification"] == "policy_denied"


def test_edited_arguments_are_the_only_source_of_the_write() -> None:
    container, store, _publisher, knowledge = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    edited = dict(approval.arguments)
    edited["title"] = "改过的标题"
    edited["start_time"] = "2026-09-27T09:30:00+08:00"
    edited.pop("time_expression", None)

    approve(container, store, approval, arguments=edited)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    call = knowledge.calendar_calls[0]
    assert call["title"] == "改过的标题"
    assert call["start_time"] == "2026-09-27T01:30:00Z"
    assert call["request_id"] == f"chat:user-1:{task.task_id}"


def test_unbound_calendar_waits_then_reuses_the_same_request_id() -> None:
    client = FakeKnowledgeClient(calendar_error=CalendarNotBound("no calendar"))
    container, store, _publisher, _client = build_step2_container(knowledge=client)
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)

    assert container.execution_service.run_task(task.task_id) == "waiting_input"
    waiting = [event for event in store.list_events(task.task_id) if event.event_type == "task.waiting_input"]
    assert waiting[-1].payload["missing_information"] == ["calendar_authorization"]
    assert client.calendar_calls[0]["request_id"] == f"chat:user-1:{task.task_id}"

    # The user binds a calendar and supplies the input again: the Task continues,
    # and the external request id is unchanged so nothing is written twice.
    client.calendar_error = None
    container.task_service.submit_input(
        task.task_id, owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    second = drive_to_approval(container, store, task.task_id)
    approve(container, store, second)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    assert len(client.calendar_calls) == 2
    assert {call["request_id"] for call in client.calendar_calls} == {f"chat:user-1:{task.task_id}"}
    assert store.get_task(task.task_id).task_id == task.task_id


def test_unknown_external_result_is_terminal_and_not_retried() -> None:
    client = FakeKnowledgeClient(calendar_error=CalendarResultUnknown("timeout"))
    container, store, _publisher, _client = build_step2_container(knowledge=client)
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)

    assert container.execution_service.run_task(task.task_id) == "unknown"

    stored = store.get_task(task.task_id)
    assert stored.last_error["classification"] == "unknown_external_result"
    assert len(client.calendar_calls) == 1
    assert store.list_observations(task.task_id)[0].status == "unknown"


def test_repeating_the_same_confirmation_creates_no_second_call() -> None:
    from app.kernel.approval import ApprovalError

    container, store, _publisher, knowledge = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    with pytest.raises(ApprovalError):
        approve(container, store, approval)

    assert len(knowledge.calendar_calls) == 1
    assert len(store.list_observations(task.task_id)) == 1
