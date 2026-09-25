"""End-to-end Outbox -> Redis Stream -> Worker test.

Skipped unless AGENT_TEST_DATABASE_URL and AGENT_TEST_REDIS_URL are set. The
test uses an isolated stream name and deletes everything it created.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.config import Settings

TEST_DATABASE_URL = os.getenv("AGENT_TEST_DATABASE_URL", "")
TEST_REDIS_URL = os.getenv("AGENT_TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="AGENT_TEST_DATABASE_URL and AGENT_TEST_REDIS_URL are required",
)


def test_wakeup_travels_through_redis_and_completes_the_task() -> None:
    from app.container import build_container
    from app.infrastructure.postgres.connection import build_pool
    from app.infrastructure.postgres.store import PostgresAgentStore
    from app.infrastructure.redis.connection import build_redis
    from app.infrastructure.redis.streams import RedisTaskPublisher, RedisTaskWorker
    from app.testing.fake_capabilities import FakeReadCapability

    suffix = uuid.uuid4().hex[:8]
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        database_schema="agent",
        redis_url=TEST_REDIS_URL,
        redis_inbound_stream=f"agent:tasks:test-{suffix}",
        redis_consumer_group=f"agent-workers-test-{suffix}",
        redis_consumer_name="test-consumer",
        redis_block_ms=500,
    )
    pool = build_pool(settings)
    store = PostgresAgentStore(pool, schema="agent")
    client = build_redis(settings)
    publisher = RedisTaskPublisher(client, settings.redis_inbound_stream)
    container = build_container(settings=settings, store=store, publisher=publisher)

    task_id = ""
    try:
        task = container.task_service.create_task(
            owner_user_id="user-1",
            payload={"text": "ping", "steps": [{"capability": "fake.read", "arguments": {"value": "ping"}}]},
        )
        task_id = task.task_id

        assert container.execution_service.dispatch_outbox() >= 1

        worker = RedisTaskWorker(
            container.execution_service.handle_wakeup, client=client, settings=settings
        )
        handled = worker.run_once()
        assert handled >= 1

        stored = store.get_task(task_id)
        assert stored is not None
        assert stored.status == "succeeded"
        events = [event.event_type for event in store.list_events(task_id)]
        assert events[0] == "task.accepted"
        assert events[-1] == "task.completed"
        assert isinstance(FakeReadCapability(), object)
    finally:
        if task_id:
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM agent.agent_outbox_events WHERE task_id = %s", (task_id,))
                    cursor.execute("DELETE FROM agent.agent_tasks WHERE task_id = %s", (task_id,))
        try:
            client.delete(settings.redis_inbound_stream)
            client.delete(settings.redis_outbound_stream)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
        pool.close()


def test_unacked_message_is_reclaimed_after_worker_crash() -> None:
    """A crash before ACK leaves the entry pending; a new worker must reclaim it."""

    from app.container import build_container
    from app.infrastructure.postgres.connection import build_pool
    from app.infrastructure.postgres.store import PostgresAgentStore
    from app.infrastructure.redis.connection import build_redis
    from app.infrastructure.redis.streams import RedisTaskPublisher, RedisTaskWorker

    suffix = uuid.uuid4().hex[:8]
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        database_schema="agent",
        redis_url=TEST_REDIS_URL,
        redis_inbound_stream=f"agent:tasks:crash-{suffix}",
        redis_consumer_group=f"agent-workers-crash-{suffix}",
        redis_consumer_name="crashed-consumer",
        redis_block_ms=200,
        redis_claim_idle_ms=0,
    )
    pool = build_pool(settings)
    store = PostgresAgentStore(pool, schema="agent")
    client = build_redis(settings)
    publisher = RedisTaskPublisher(client, settings.redis_inbound_stream)
    container = build_container(settings=settings, store=store, publisher=publisher)

    task_id = ""
    try:
        task = container.task_service.create_task(
            owner_user_id="user-1",
            payload={"text": "reclaim", "steps": [{"capability": "fake.read", "arguments": {"value": "reclaim"}}]},
        )
        task_id = task.task_id
        container.execution_service.dispatch_outbox()

        def crashing_handler(_task_id: str) -> None:
            raise RuntimeError("worker crashed before ACK")

        crashed = RedisTaskWorker(crashing_handler, client=client, settings=settings)
        assert crashed.run_once() == 0
        pending = client.xpending(settings.redis_inbound_stream, settings.redis_consumer_group)
        assert pending["pending"] >= 1

        resumed_settings = Settings(
            database_url=TEST_DATABASE_URL,
            database_schema="agent",
            redis_url=TEST_REDIS_URL,
            redis_inbound_stream=settings.redis_inbound_stream,
            redis_consumer_group=settings.redis_consumer_group,
            redis_consumer_name="resumed-consumer",
            redis_block_ms=200,
            redis_claim_idle_ms=0,
        )
        resumed = RedisTaskWorker(
            container.execution_service.handle_wakeup, client=client, settings=resumed_settings
        )
        assert resumed.run_once() >= 1

        stored = store.get_task(task_id)
        assert stored is not None
        assert stored.status == "succeeded"
        assert client.xpending(settings.redis_inbound_stream, settings.redis_consumer_group)["pending"] == 0
    finally:
        if task_id:
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM agent.agent_outbox_events WHERE task_id = %s", (task_id,))
                    cursor.execute("DELETE FROM agent.agent_tasks WHERE task_id = %s", (task_id,))
        try:
            client.delete(settings.redis_inbound_stream)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
        pool.close()
