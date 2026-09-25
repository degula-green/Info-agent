"""PostgreSQL store integration tests.

Skipped unless AGENT_TEST_DATABASE_URL points at a disposable database that has
already run db/migrations/20260925_agent_runtime_rebuild.sql.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from app.kernel.models import TaskEvent, TaskRecord

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
