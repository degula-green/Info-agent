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
    from app.kernel.registry import CapabilityRegistry
    from app.testing.fake_capabilities import FakeReadCapability
    from app.testing.fake_planner import InputDrivenFakePlanner

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
    registry = CapabilityRegistry([FakeReadCapability()])
    container = build_container(
        settings=settings,
        store=store,
        publisher=publisher,
        registry=registry,
        planner=InputDrivenFakePlanner(),
    )

    task_id = ""
    try:
        task = container.task_service.create_task(
            owner_user_id="user-1",
            payload={"text": "ping", "steps": [{"capability": "fake.read", "arguments": {"value": "ping"}}]},
        )
        task_id = task.task_id

        # A real worker process may dispatch the wake-up first, so assert on the
        # outcome (the signal travels and the Task completes) instead of on who
        # published it.
        container.execution_service.dispatch_outbox()
        worker = RedisTaskWorker(
            container.execution_service.handle_wakeup, client=client, settings=settings
        )
        stored = None
        for _ in range(40):
            worker.run_once()
            stored = store.get_task(task_id)
            if stored is not None and stored.status in {"succeeded", "failed"}:
                break
        assert stored is not None
        assert stored.status == "succeeded"
        events = [event.event_type for event in store.list_events(task_id)]
        assert events[0] == "task.accepted"
        assert events[-1] == "task.completed"
        assert [item.capability for item in store.list_observations(task_id)] == ["fake.read"]
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

    from app.kernel.registry import CapabilityRegistry
    from app.testing.fake_capabilities import FakeReadCapability
    from app.testing.fake_planner import InputDrivenFakePlanner

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
    registry = CapabilityRegistry([FakeReadCapability()])
    container = build_container(
        settings=settings,
        store=store,
        publisher=publisher,
        registry=registry,
        planner=InputDrivenFakePlanner(),
    )

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
        assert [item.capability for item in store.list_observations(task_id)] == ["fake.read"]
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


def test_knowledge_consumer_does_not_replay_the_historical_backlog() -> None:
    """The Agent group is created at "$": only messages published after it are seen."""

    import json

    from app.infrastructure.redis.connection import build_redis
    from app.infrastructure.redis.knowledge_consumer import RedisKnowledgeEventWorker
    from tests.support import knowledge_event

    suffix = uuid.uuid4().hex[:8]
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        database_schema="agent",
        redis_url=TEST_REDIS_URL,
        redis_block_ms=200,
    )
    stream = f"knowledge:ready:test-{suffix}"
    group = f"agent-workers-test-{suffix}"
    client = build_redis(settings)
    seen: list[str] = []
    try:
        # Published before the Agent group exists: this is the historical backlog.
        client.xadd(stream, {"event": json.dumps(knowledge_event(knowledge_item_id="item-historical"))})

        worker = RedisKnowledgeEventWorker(
            lambda payload: seen.append(payload["payload"]["knowledge_item_id"]) is None,
            client=client,
            stream=stream,
            group=group,
            consumer="test-consumer",
            block_ms=200,
        )
        assert worker.run_once() == 0
        assert seen == []

        client.xadd(stream, {"event": json.dumps(knowledge_event(knowledge_item_id="item-fresh"))})
        assert worker.run_once() == 1
        assert seen == ["item-fresh"]
        assert client.xpending(stream, group)["pending"] == 0
    finally:
        try:
            client.delete(stream)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
