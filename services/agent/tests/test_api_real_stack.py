"""HTTP API smoke test against the real PostgreSQL and Redis stack.

Skipped unless AGENT_TEST_DATABASE_URL and AGENT_TEST_REDIS_URL are set. The
Task row created here is deleted afterwards.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.getenv("AGENT_TEST_DATABASE_URL", "")
TEST_REDIS_URL = os.getenv("AGENT_TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="AGENT_TEST_DATABASE_URL and AGENT_TEST_REDIS_URL are required",
)


def test_api_startup_wires_real_store_and_serves_a_task() -> None:
    os.environ.setdefault("AGENT_DATABASE_URL", TEST_DATABASE_URL)
    os.environ.setdefault("AGENT_REDIS_URL", TEST_REDIS_URL)

    from app.main import app
    from app.routers import tasks

    headers = {"X-Agent-User-Id": "user-1"}
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"

        created = client.post(
            "/api/agent/v1/tasks",
            json={"text": "smoke", "steps": [{"capability": "fake.read", "arguments": {"value": "smoke"}}]},
            headers=headers,
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        container = tasks.get_container()
        try:
            run = client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=headers)
            assert run.status_code == 200
            assert run.json()["status"] == "succeeded"

            stored = container.store.get_task(task_id)
            assert stored is not None and stored.status == "succeeded"
            events = [event.event_type for event in container.store.list_events(task_id)]
            assert events[0] == "task.accepted"
            assert events[-1] == "task.completed"
        finally:
            pool = getattr(container.store, "pool", None)
            if pool is not None:
                with pool.connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM agent.agent_outbox_events WHERE task_id = %s", (task_id,)
                        )
                        cursor.execute("DELETE FROM agent.agent_tasks WHERE task_id = %s", (task_id,))
