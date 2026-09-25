"""HTTP API smoke test against the real PostgreSQL and Redis stack.

Skipped unless AGENT_TEST_DATABASE_URL and AGENT_TEST_REDIS_URL are set. It
starts the real application container (deterministic planner, descriptor policy
and the calendar capability) and deletes every Task row it created.
"""

from __future__ import annotations

import importlib
import os
import time

import pytest
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.getenv("AGENT_TEST_DATABASE_URL", "")
TEST_REDIS_URL = os.getenv("AGENT_TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="AGENT_TEST_DATABASE_URL and AGENT_TEST_REDIS_URL are required",
)


def test_api_startup_wires_the_real_agent_stack() -> None:
    # ``app.config.settings`` is built at import time, so the test environment
    # must be in place before the application module is (re)loaded.
    os.environ["AGENT_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["AGENT_REDIS_URL"] = TEST_REDIS_URL
    importlib.reload(importlib.import_module("app.config"))
    main_module = importlib.reload(importlib.import_module("app.main"))

    from app.infrastructure.postgres.store import PostgresAgentStore
    from app.infrastructure.redis.streams import RedisTaskPublisher
    from app.routers import tasks

    headers = {"X-Agent-User-Id": "user-1"}
    with TestClient(main_module.app) as client:
        assert client.get("/health").json()["status"] == "ok"
        container = tasks.get_container()
        assert isinstance(container.store, PostgresAgentStore)
        assert isinstance(container.publisher, RedisTaskPublisher)

        def wait_for_status(task_id: str, wanted: set[str], timeout_seconds: float = 20.0) -> str:
            """A worker may own the lease, so poll instead of trusting one /run reply."""

            deadline = time.monotonic() + timeout_seconds
            status = ""
            while time.monotonic() < deadline:
                status = client.get(f"/api/agent/v1/tasks/{task_id}", headers=headers).json()["status"]
                if status in wanted:
                    return status
                client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=headers)
                time.sleep(0.2)
            return status

        created_ids: list[str] = []
        try:
            # A schedule request plans a real calendar step and waits for approval.
            created = client.post(
                "/api/agent/v1/tasks",
                json={"text": "明天晚上八点开个评审会"},
                headers=headers,
            )
            assert created.status_code == 202
            task_id = created.json()["task_id"]
            created_ids.append(task_id)

            assert client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=headers).status_code == 200
            assert wait_for_status(task_id, {"waiting_approval"}) == "waiting_approval"

            plan = client.get(f"/api/agent/v1/tasks/{task_id}/plan", headers=headers).json()["plan"]
            assert [step["capability"] for step in plan["steps"]] == ["calendar.create"]
            approvals = client.get("/api/agent/v1/approvals", headers=headers).json()["items"]
            assert [item["capability"] for item in approvals if item["task_id"] == task_id] == [
                "calendar.create"
            ]

            # Plain chat has nothing to execute and completes immediately.
            plain = client.post(
                "/api/agent/v1/tasks", json={"text": "晚上好"}, headers=headers
            )
            plain_id = plain.json()["task_id"]
            created_ids.append(plain_id)
            client.post(f"/api/agent/v1/tasks/{plain_id}/run", headers=headers)
            assert wait_for_status(plain_id, {"succeeded"}) == "succeeded"
            events = [event.event_type for event in container.store.list_events(plain_id)]
            assert events[0] == "task.accepted"
            assert events[-1] == "task.completed"
            assert container.store.list_observations(plain_id) == []
        finally:
            pool = getattr(container.store, "pool", None)
            if pool is not None and created_ids:
                with pool.connection() as connection:
                    with connection.cursor() as cursor:
                        for created_id in created_ids:
                            cursor.execute(
                                "DELETE FROM agent.agent_outbox_events WHERE task_id = %s",
                                (created_id,),
                            )
                            cursor.execute(
                                "DELETE FROM agent.agent_tasks WHERE task_id = %s", (created_id,)
                            )
