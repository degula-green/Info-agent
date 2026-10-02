"""PostgreSQL store integration tests.

Skipped unless AGENT_TEST_DATABASE_URL points at a disposable database that has
already run db/migrations/20260925_agent_runtime_rebuild.sql and
db/migrations/20260927_agent_dynamic_plan.sql through
db/migrations/20261002_agent_conversation_history.up.sql.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from app.kernel.models import (
    ConversationRecord,
    MessageRecord,
    TaskEvent,
    TaskInput,
    TaskRecord,
)

TEST_DATABASE_URL = os.getenv("AGENT_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="AGENT_TEST_DATABASE_URL is not configured"
)


@pytest.fixture()
def store():
    from app.config import Settings
    from app.infrastructure.postgres.connection import build_pool
    from app.infrastructure.postgres.store import PostgresAgentStore

    settings = Settings(database_url=TEST_DATABASE_URL, database_schema="agent")
    pool = build_pool(settings)
    store = PostgresAgentStore(pool, schema="agent")
    created: list[str] = []
    store.created_task_ids = created  # type: ignore[attr-defined]
    yield store
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM agent.agent_tasks WHERE task_id = ANY(%s)",
                (created,),
            )
    pool.close()


def _record() -> TaskRecord:
    moment = datetime.now(timezone.utc)
    return TaskRecord(
        task_id=str(uuid.uuid4()),
        source_type="chat",
        owner_user_id="user-1",
        input={"text": "hello"},
        created_at=moment,
        updated_at=moment,
    )


def test_task_state_events_and_outbox_commit_atomically(store) -> None:
    task = _record()
    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task.task_id,
        sequence=0,
        event_type="task.accepted",
        payload={},
        occurred_at=datetime.now(timezone.utc),
    )
    stored = store.create_task(task, events=[event], outbox_events=[])
    store.created_task_ids.append(task.task_id)
    assert stored.task_id == task.task_id

    events = store.list_events(task.task_id)
    assert events[0].sequence == 1

    task.status = "planning"
    second = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task.task_id,
        sequence=0,
        event_type="task.planning",
        payload={},
        occurred_at=datetime.now(timezone.utc),
    )
    store.commit(task, events=[second])
    assert [item.event_type for item in store.list_events(task.task_id)] == [
        "task.accepted",
        "task.planning",
    ]
    assert store.get_task(task.task_id).status == "planning"


def test_idempotency_key_prevents_duplicate_tasks(store) -> None:
    task = _record()
    task.idempotency_key = "chat:user-1:msg-1"
    first = store.create_task(task, events=[], outbox_events=[])
    store.created_task_ids.append(first.task_id)
    duplicate = _record()
    duplicate.idempotency_key = "chat:user-1:msg-1"
    second = store.create_task(duplicate, events=[], outbox_events=[])
    store.created_task_ids.append(second.task_id)
    assert first.task_id == second.task_id
    assert store.find_task_by_idempotency_key("chat:user-1:msg-1").task_id == first.task_id


def test_conversation_task_and_messages_commit_together(store) -> None:
    moment = datetime.now(timezone.utc)
    conversation_id = str(uuid.uuid4())
    request_message_id = str(uuid.uuid4())
    response_message_id = str(uuid.uuid4())
    task = _record()
    task.conversation_id = conversation_id
    task.request_message_id = request_message_id
    task.response_message_id = response_message_id
    conversation = ConversationRecord(
        conversation_id=conversation_id,
        owner_user_id=task.owner_user_id,
        title="history",
        created_at=moment,
        updated_at=moment,
    )
    messages = [
        MessageRecord(
            message_id=request_message_id,
            conversation_id=conversation_id,
            role="user",
            content="hello",
            status="completed",
            task_id=task.task_id,
            created_at=moment,
            updated_at=moment,
        ),
        MessageRecord(
            message_id=response_message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="",
            status="pending",
            task_id=task.task_id,
            created_at=moment,
            updated_at=moment,
        ),
    ]
    input_item = TaskInput(
        input_id=str(uuid.uuid4()),
        task_id=task.task_id,
        version=1,
        payload={"text": "hello"},
        created_at=moment,
    )

    stored = store.create_task(
        task,
        events=[],
        outbox_events=[],
        inputs=[input_item],
        conversation=conversation,
        messages=messages,
    )
    store.created_task_ids.append(task.task_id)

    assert stored.conversation_id == conversation_id
    assert store.get_conversation(conversation_id).title == "history"
    assert [item.role for item in store.list_messages(conversation_id)] == [
        "user",
        "assistant",
    ]
    assert store.list_inputs(task.task_id)[0].payload == {"text": "hello"}

    assert store.delete_conversation(
        conversation_id,
        owner_user_id=task.owner_user_id,
    )
    assert store.get_conversation(conversation_id) is None
    assert store.list_messages(conversation_id) == []


def test_task_payload_survives_a_round_trip(store) -> None:
    """input / source_ref / constraints must be persisted, not just kept in memory."""

    task = _record()
    task.input = {"text": "明天晚上八点开个评审会"}
    task.source_ref = {"knowledge_item_id": "item-1", "content_version": 3}
    task.constraints = {"timezone": "Asia/Shanghai"}
    store.create_task(task, events=[], outbox_events=[])
    store.created_task_ids.append(task.task_id)

    reloaded = store.get_task(task.task_id)
    assert reloaded.input == {"text": "明天晚上八点开个评审会"}
    assert reloaded.source_ref == {"knowledge_item_id": "item-1", "content_version": 3}
    assert reloaded.constraints == {"timezone": "Asia/Shanghai"}

    # A later commit (user supplied input) must persist the merged payload too.
    reloaded.input = {**reloaded.input, "text": "明天晚上九点"}
    store.commit(reloaded)
    assert store.get_task(task.task_id).input == {"text": "明天晚上九点"}


def test_replanning_takes_the_next_plan_version(store) -> None:
    """``agent_plans`` is unique on (task_id, version).

    Re-planning after user input must therefore allocate a fresh version: reusing
    the replaced Plan's version made the INSERT fail and left the Task stuck in
    "planning" while the worker retried forever.
    """

    from app.kernel.models import Plan

    task = _record()
    store.create_task(task, events=[], outbox_events=[])
    store.created_task_ids.append(task.task_id)

    first = Plan(plan_id=str(uuid.uuid4()), task_id=task.task_id, objective="first")
    first.version = store.next_plan_version(task.task_id)
    store.save_plan(first)

    second = Plan(plan_id=str(uuid.uuid4()), task_id=task.task_id, objective="second")
    second.version = store.next_plan_version(task.task_id)
    store.save_plan(second)

    assert (first.version, second.version) == (1, 2)
    assert store.get_active_plan(task.task_id).plan_id == second.plan_id


def test_dynamic_planning_fields_survive_a_round_trip(store) -> None:
    from app.kernel.models import Plan, PlanStep

    task = _record()
    task.understanding = {
        "is_task": True,
        "goal": "create a todo",
        "task_kind": "action",
        "intent_candidates": [
            {"name": "todo.create", "confidence": 0.9, "evidence": "开会"}
        ],
        "confidence": 0.9,
        "reason": "test",
    }
    task.result = {"warnings": ["unsupported intent: web.research"]}
    task.replan_count = 1
    task.step_count = 2
    task.model_call_count = 3
    store.create_task(task, events=[], outbox_events=[])
    store.created_task_ids.append(task.task_id)

    reloaded = store.get_task(task.task_id)
    assert reloaded.understanding == task.understanding
    assert reloaded.result == task.result
    assert reloaded.replan_count == 1
    assert reloaded.step_count == 2
    assert reloaded.model_call_count == 3

    parent = Plan(
        plan_id=str(uuid.uuid4()),
        task_id=task.task_id,
        version=1,
        objective="parent",
    )
    store.save_plan(parent)
    child = Plan(
        plan_id=str(uuid.uuid4()),
        task_id=task.task_id,
        version=2,
        parent_plan_id=parent.plan_id,
        triggered_by_observation_id="obs-1",
        replan_reason="retryable failure",
        unsupported_intents=["web.research"],
        warnings=["unsupported intent: web.research"],
        requires_user_confirmation=True,
        objective="child",
        steps=[
            PlanStep(
                step_id=str(uuid.uuid4()),
                plan_id="pending",
                order=1,
                capability="fake.read",
                arguments={"value": "x"},
            )
        ],
    )
    child.steps[0].plan_id = child.plan_id
    store.save_plan(child)
    store.save_steps(task.task_id, child.steps)

    stored_child = store.get_active_plan(task.task_id)
    assert stored_child.parent_plan_id == parent.plan_id
    assert stored_child.triggered_by_observation_id == "obs-1"
    assert stored_child.replan_reason == "retryable failure"
    assert stored_child.unsupported_intents == ["web.research"]
    assert stored_child.warnings == ["unsupported intent: web.research"]
    assert stored_child.requires_user_confirmation is True


def test_observation_evidence_and_evidence_rows_round_trip(store) -> None:
    """The provenance a capability returned has to survive a restart."""

    from app.kernel.models import EvidenceRecord, Observation

    task = _record()
    store.create_task(task, events=[], outbox_events=[])
    store.created_task_ids.append(task.task_id)

    observation = Observation(
        observation_id=str(uuid.uuid4()),
        task_id=task.task_id,
        plan_id="plan-1",
        step_id="plan-1-step-2",
        capability="web.extract",
        status="succeeded",
        output={"title": "示例页面"},
        evidence=[
            {
                "evidence_id": "ev-abc",
                "source": "web",
                "url": "https://93.184.216.34/page",
                "snippet": "正文片段",
                "version": 1,
            }
        ],
        created_at=datetime.now(timezone.utc),
    )
    store.save_observation(observation)

    record = EvidenceRecord(
        evidence_id=f"{observation.observation_id}:ev-abc",
        task_id=task.task_id,
        plan_id=observation.plan_id,
        step_id=observation.step_id,
        observation_id=observation.observation_id,
        payload=observation.evidence[0],
        created_at=datetime.now(timezone.utc),
    )
    store.save_evidence(record)
    # A retry of the same step must not raise or duplicate the row.
    store.save_evidence(record)

    reloaded = [
        item
        for item in store.list_observations(task.task_id)
        if item.observation_id == observation.observation_id
    ][0]
    assert reloaded.evidence[0]["evidence_id"] == "ev-abc"
    assert reloaded.evidence[0]["url"] == "https://93.184.216.34/page"
