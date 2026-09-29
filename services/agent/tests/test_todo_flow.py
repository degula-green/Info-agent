"""Stage 5: Task -> Plan -> Approval -> Execute -> Observation, to-do edition.

This is the same loop the calendar version drove, but the write now lands in
the Agent's own to-do ledger instead of a vendor calendar. It is the test that
proves the merged product works end to end: one intent, one capability, time
optional, and the preview only becomes a row after the owner confirms.
"""

from __future__ import annotations

import pytest

from tests.support import build_step2_container, event_types, knowledge_event, snapshot
from app.testing.fake_knowledge import FakeKnowledgeClient


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


def todos(container, owner: str = "user-1"):
    return container.todo_store.list_todos(owner)


def test_chat_request_creates_one_todo_after_confirmation() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )

    approval = drive_to_approval(container, store, task.task_id)
    # Nothing is written before the user confirms: the preview is a draft.
    assert todos(container) == []
    assert approval.capability == "todo.create"
    assert approval.arguments["title"] == "开个评审会"
    assert approval.arguments["due_expression"] == "明天晚上八点"

    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    created = todos(container)
    assert len(created) == 1
    todo = created[0]
    assert todo.title == "开个评审会"
    assert todo.status == "open"
    assert todo.due_at is not None
    assert todo.idempotency_key == f"chat:user-1:{task.task_id}"

    events = event_types(store, task.task_id)
    assert events[0] == "task.accepted"
    assert events[-1] == "task.completed"
    assert events.index("task.waiting_approval") < events.index("step.started")
    assert len(store.list_observations(task.task_id)) == 1


def test_a_todo_without_a_time_needs_no_extra_input() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "完成登录模块代码"}
    )

    # A missing time is not a missing input any more: it goes straight to the
    # approval instead of pausing the Task.
    approval = drive_to_approval(container, store, task.task_id)
    assert approval.arguments["title"] == "完成登录模块代码"
    assert "due_at" not in approval.arguments

    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    todo = todos(container)[0]
    assert todo.title == "完成登录模块代码"
    assert todo.due_at is None
    assert todo.due_expression is None


def test_an_ambiguous_time_becomes_an_unresolved_todo_not_a_question() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天八点开个评审会"}
    )

    approval = drive_to_approval(container, store, task.task_id)
    assert "due_at" not in approval.arguments
    assert approval.arguments["due_expression"] == "明天八点"

    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    todo = todos(container)[0]
    assert todo.due_at is None
    # The phrase is preserved so the desktop can still show what was written.
    assert todo.due_expression == "明天八点"


def test_plain_chat_completes_without_any_capability_call() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(owner_user_id="user-1", payload={"text": "晚上好"})

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    assert todos(container) == []
    assert store.list_observations(task.task_id) == []
    assert "task.waiting_approval" not in event_types(store, task.task_id)


def test_collected_message_fans_out_and_each_owner_gets_its_own_todo() -> None:
    client = FakeKnowledgeClient(
        snapshots={
            "item-1": snapshot(
                conversation_type="group",
                eligible_owners=[
                    {"owner_user_id": "user-1"},
                    {"owner_user_id": "user-2"},
                ],
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

    assert [todo.title for todo in todos(container, "user-1")] == ["开个评审会，会议室 A"]
    assert [todo.title for todo in todos(container, "user-2")] == ["开个评审会，会议室 A"]
    keys = {
        todo.idempotency_key
        for owner in ("user-1", "user-2")
        for todo in todos(container, owner)
    }
    assert keys == {
        "knowledge_event:user-1:item-1:1",
        "knowledge_event:user-2:item-1:1",
    }


def test_rejecting_the_approval_fails_the_task_without_writing() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)

    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=False
    )
    assert container.execution_service.run_task(task.task_id) == "failed"

    assert todos(container) == []
    assert store.get_task(task.task_id).last_error["classification"] == "policy_denied"


def test_edited_arguments_are_the_only_source_of_the_write() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    edited = dict(approval.arguments)
    edited["title"] = "改过的标题"
    edited["due_expression"] = "下周三下午"
    edited.pop("due_at", None)

    approve(container, store, approval, arguments=edited)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    todo = todos(container)[0]
    assert todo.title == "改过的标题"
    # The desktop edit cleared the resolved moment, so the row keeps the phrase.
    assert todo.due_expression == "下周三下午"
    assert todo.idempotency_key == f"chat:user-1:{task.task_id}"


def test_the_preview_is_superseded_by_the_real_todo() -> None:
    """The client stops showing a draft once the to-do exists.

    The draft is the Task's Plan plus its Approval; the to-do is a ledger row.
    They are different objects with different lifetimes, so this asserts the
    client-visible signals: nothing is still pending approval, and the thing the
    draft promised now exists.
    """

    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    # Nothing is pending approval, so no client is still showing a draft.
    assert [
        item
        for item in store.list_approvals(task_id=task.task_id)
        if item.status == "waiting_approval"
    ] == []
    # And the draft really became the thing it previewed.
    created = todos(container)
    assert len(created) == 1
    assert created[0].title == approval.arguments["title"]
    assert created[0].due_expression == approval.arguments["due_expression"]


# -- step 7: the preview lifecycle -------------------------------------------
# The draft a client shows and the to-do row are different objects with
# different lifetimes. These pin the handover: the preview is announced once,
# only when a real row exists, and never on a rejected draft.


def test_confirming_a_draft_announces_the_preview_handover() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )

    # Before approval there is no row, so there is nothing to announce.
    approval = drive_to_approval(container, store, task.task_id)
    assert "task.preview_confirmed" not in event_types(store, task.task_id)

    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    events = [item for item in store.list_events(task.task_id) if item.event_type == "task.preview_confirmed"]
    assert len(events) == 1
    payload = events[0].payload
    created = todos(container)
    assert len(created) == 1
    # The announcement names the row that now exists, not the draft that did not.
    assert payload["todo_id"] == created[0].todo_id
    assert payload["title"] == created[0].title
    assert payload["due_expression"] == "明天晚上八点"
    assert payload["step_id"]


def test_a_rejected_draft_announces_no_preview() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)

    container.execution_service.approval_gateway.decide(
        approval.approval_id,
        owner_user_id="user-1",
        approve=False,
    )
    container.execution_service.run_task(task.task_id)

    assert todos(container) == []
    # No row was written, so no draft may claim it became one.
    assert "task.preview_confirmed" not in event_types(store, task.task_id)


def test_the_handover_is_announced_once_per_draft() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    # Re-running a finished task must not re-announce the same handover.
    container.execution_service.run_task(task.task_id)

    assert event_types(store, task.task_id).count("task.preview_confirmed") == 1
    assert len(todos(container)) == 1


def test_a_second_confirmation_creates_no_second_todo() -> None:
    from app.kernel.approval import ApprovalError

    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开个评审会"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    with pytest.raises(ApprovalError):
        approve(container, store, approval)

    assert len(todos(container)) == 1
    assert len(store.list_observations(task.task_id)) == 1


def test_an_overdue_todo_stays_open_and_listed() -> None:
    """Nothing expires a to-do: only the owner deletes or finishes it."""

    from datetime import datetime, timedelta, timezone

    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "补交昨天没交的报告"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace(
        "+00:00", "Z"
    )
    edited = dict(approval.arguments)
    edited["due_at"] = past
    edited.pop("due_expression", None)
    approve(container, store, approval, arguments=edited)
    assert container.execution_service.run_task(task.task_id) == "succeeded"

    listed = todos(container)
    assert len(listed) == 1
    assert listed[0].status == "open"
    assert listed[0].due_at is not None and listed[0].due_at < datetime.now(timezone.utc)


def test_the_desktop_can_edit_then_delete_the_todo() -> None:
    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "完成登录模块代码"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    approve(container, store, approval)
    container.execution_service.run_task(task.task_id)
    todo = todos(container)[0]

    updated = container.todo_store.update_todo(
        todo.todo_id,
        owner_user_id="user-1",
        changes={"title": "完成登录模块代码并自测", "status": "done"},
    )
    assert updated is not None
    assert updated.title == "完成登录模块代码并自测"
    assert updated.status == "done"
    assert updated.completed_at is not None

    # Deletion is the owner's action and removes the row for good.
    assert container.todo_store.delete_todo(todo.todo_id, owner_user_id="user-1") is True
    assert todos(container) == []
    assert container.todo_store.get_todo(todo.todo_id) is None


def _lapse(store, approval):
    """Push the approval window into the past, as hours of waiting would."""

    from datetime import datetime, timedelta, timezone

    stored = store.get_approval(approval.approval_id)
    stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.save_approval(stored)
    return store.get_approval(approval.approval_id)


def test_a_lapsed_approval_window_still_confirms_the_same_preview() -> None:
    """The owner confirming hours later must not hit "approval has expired".

    The window bounds how long a *pending* decision may sit; it must not turn
    an untouched preview into a dead card, because the argument fingerprint
    below still proves the owner confirmed exactly what they were shown.
    """

    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "完成登录模块代码"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    lapsed = _lapse(store, approval)
    assert lapsed.expires_at is not None

    decided = approve(container, store, approval)
    assert decided.status == "approved"
    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert len(todos(container)) == 1


def test_a_lapsed_approval_still_requires_confirmation_after_arguments_change() -> None:
    """Expiry must never let an old decision authorise rewritten arguments."""

    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "完成登录模块代码"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    _lapse(store, approval)

    step = store.get_step(approval.step_id)
    step.arguments = {**step.arguments, "title": "被悄悄改掉的标题"}
    store.update_step(step)

    with pytest.raises(Exception) as failure:
        approve(container, store, approval)
    assert "no longer matches the step arguments" in str(failure.value)
    assert todos(container) == []


def test_a_cancelled_task_cannot_be_confirmed_by_a_late_click() -> None:
    """A late click on a cancelled draft must fail cleanly, not crash.

    The window-revival path treats an expired approval as still decidable, so
    the Task status has to be checked first: a cancelled Task has no legal
    transition back to ready, and letting it reach the transition guard would
    turn a plain refusal into a 500.
    """

    container, store, _publisher, _client = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "完成登录模块代码"}
    )
    approval = drive_to_approval(container, store, task.task_id)
    _lapse(store, approval)
    container.task_service.cancel(task.task_id, owner_user_id="user-1")

    with pytest.raises(Exception) as failure:
        approve(container, store, approval)
    assert "approval is not pending" in str(failure.value)
    assert todos(container) == []
