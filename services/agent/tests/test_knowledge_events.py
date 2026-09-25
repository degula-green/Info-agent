"""knowledge.ready handling: snapshot decisions, fan-out and ACK/retry rules."""

from __future__ import annotations

import json

from app.application.knowledge_events import KnowledgeEventService
from app.infrastructure.knowledge.client import (
    KnowledgeForbidden,
    KnowledgeItemNotFound,
    KnowledgeItemNotReady,
    KnowledgeUnavailable,
)
from app.infrastructure.redis.knowledge_consumer import RedisKnowledgeEventWorker
from app.testing.fake_knowledge import FakeKnowledgeClient
from tests.support import build_step2_container, knowledge_event, snapshot


def build(*, client: FakeKnowledgeClient, platforms=None):
    container, store, _publisher, _client = build_step2_container(
        knowledge=client, **({"knowledge_platforms": platforms} if platforms else {})
    )
    return container.knowledge_events, store, container


def test_private_message_creates_exactly_one_task() -> None:
    client = FakeKnowledgeClient(snapshots={"item-1": snapshot()})
    service, store, _container = build(client=client)

    outcome = service.handle(knowledge_event())

    assert outcome.ack is True
    assert outcome.reason == "fanout"
    assert len(outcome.task_ids) == 1
    task = store.get_task(outcome.task_ids[0])
    assert task.owner_user_id == "user-1"
    assert task.source_type == "knowledge_event"
    assert task.idempotency_key == "knowledge_event:user-1:item-1:1"
    assert task.source_ref["knowledge_item_id"] == "item-1"
    assert task.source_ref["sent_at"] == "2026-09-25T06:12:30Z"
    assert task.input["text"] == "明天晚上八点开个评审会，会议室 A"


def test_group_message_fans_out_to_every_eligible_member() -> None:
    client = FakeKnowledgeClient(
        snapshots={
            "item-1": snapshot(
                conversation_type="group",
                eligible_owners=[
                    {"owner_user_id": "user-1"},
                    {"owner_user_id": "user-2"},
                    {"owner_user_id": "user-1"},
                ],
                excluded_members=[
                    {"external_user_id": "ou_1", "reason_code": "identity_unmapped"},
                    {"external_user_id": "ou_2", "reason_code": "not_a_member"},
                ],
            )
        }
    )
    service, store, _container = build(client=client)

    outcome = service.handle(knowledge_event())

    assert len(outcome.task_ids) == 2
    owners = {store.get_task(task_id).owner_user_id for task_id in outcome.task_ids}
    assert owners == {"user-1", "user-2"}
    assert set(outcome.reason_codes) == {"identity_unmapped", "not_a_member"}
    assert store.get_task(outcome.task_ids[0]).idempotency_key in {
        "knowledge_event:user-1:item-1:1",
        "knowledge_event:user-2:item-1:1",
    }


def test_replaying_the_same_event_does_not_duplicate_tasks() -> None:
    client = FakeKnowledgeClient(snapshots={"item-1": snapshot()})
    service, store, _container = build(client=client)

    first = service.handle(knowledge_event())
    second = service.handle(knowledge_event(event_id="event-2"))

    assert first.task_ids == second.task_ids
    assert len(store.list_unfinished_tasks(limit=50)) == 1


def test_attachment_items_are_skipped_without_a_snapshot_call() -> None:
    client = FakeKnowledgeClient(snapshots={"item-1": snapshot()})
    service, _store, _container = build(client=client)

    outcome = service.handle(knowledge_event(attachment_id="attachment-1"))

    assert outcome.reason == "attachment_item"
    assert client.snapshot_calls == []
    assert outcome.task_ids == ()


def test_attachment_snapshot_is_skipped() -> None:
    client = FakeKnowledgeClient(
        snapshots={"item-1": snapshot(source_attachment_id="attachment-1")}
    )
    service, _store, _container = build(client=client)

    outcome = service.handle(knowledge_event())

    assert outcome.reason == "attachment_item"
    assert outcome.task_ids == ()


def test_unresolved_visibility_is_acknowledged_without_tasks() -> None:
    client = FakeKnowledgeClient(
        snapshots={
            "item-1": snapshot(
                visibility="unknown",
                eligible_owners=[],
                excluded_members=[{"external_user_id": "ou_1", "reason_code": "membership_unknown"}],
            )
        }
    )
    service, _store, _container = build(client=client)

    outcome = service.handle(knowledge_event())

    assert outcome.ack is True
    assert outcome.reason == "visibility_unknown"
    assert outcome.task_ids == ()
    assert outcome.reason_codes == ("membership_unknown",)


def test_no_eligible_owner_is_acknowledged() -> None:
    client = FakeKnowledgeClient(snapshots={"item-1": snapshot(eligible_owners=[])})
    service, _store, _container = build(client=client)

    outcome = service.handle(knowledge_event())

    assert outcome.ack is True
    assert outcome.reason == "no_eligible_owner"
    assert outcome.task_ids == ()


def test_non_schedule_text_is_filtered_out() -> None:
    client = FakeKnowledgeClient(snapshots={"item-1": snapshot(text="哈哈哈哈")})
    service, _store, _container = build(client=client)

    outcome = service.handle(knowledge_event())

    assert outcome.ack is True
    assert outcome.reason == "prefiltered"
    assert outcome.task_ids == ()


def test_platform_outside_the_allowlist_is_filtered_out() -> None:
    client = FakeKnowledgeClient(snapshots={"item-1": snapshot(platform="dingtalk")})
    service, _store, _container = build(client=client, platforms="feishu,wechat")

    outcome = service.handle(knowledge_event())

    assert outcome.ack is True
    assert outcome.reason == "prefiltered"
    assert outcome.task_ids == ()


def test_snapshot_failures_decide_ack_or_retry() -> None:
    cases = [
        (KnowledgeItemNotFound("gone"), True, "knowledge_item_not_found"),
        (KnowledgeForbidden("no token"), True, "forbidden"),
        (KnowledgeItemNotReady("not ready"), False, "knowledge_item_not_ready"),
        (KnowledgeUnavailable("boom"), False, "knowledge_unavailable"),
    ]
    for error, expected_ack, expected_reason in cases:
        service, _store, _container = build(client=FakeKnowledgeClient(snapshot_error=error))

        outcome = service.handle(knowledge_event())

        assert outcome.ack is expected_ack, error
        assert outcome.reason == expected_reason, error


def test_unexpected_event_types_are_dropped() -> None:
    service, _store, _container = build(client=FakeKnowledgeClient())

    outcome = service.handle({"event_type": "document.extracted", "payload": {}})

    assert outcome.ack is True
    assert outcome.reason == "unexpected_event_type"


class FakeRedisStream:
    """Minimal Redis Stream double for the consumer contract."""

    def __init__(self, entries) -> None:
        self.entries = list(entries)
        self.acked: list[str] = []
        self.group_creates: list[str] = []

    def xgroup_create(self, stream, group, id=None, mkstream=False):  # noqa: A002 - redis API
        self.group_creates.append(id)

    def xreadgroup(self, group, consumer, streams, count, block):
        entries, self.entries = self.entries, []
        return [("knowledge:ready", entries)] if entries else []

    def xack(self, stream, group, message_id):
        self.acked.append(message_id)


def consumer(client, handler) -> RedisKnowledgeEventWorker:
    return RedisKnowledgeEventWorker(
        handler,
        client=client,
        stream="knowledge:ready",
        group="agent-workers",
        consumer="test-consumer",
        block_ms=0,
    )


def test_consumer_creates_the_group_at_the_stream_tail() -> None:
    client = FakeRedisStream([])
    worker = consumer(client, lambda payload: True)

    worker.ensure_group()

    assert client.group_creates == ["$"]


def test_consumer_acks_handled_entries_and_drops_unparseable_ones() -> None:
    seen: list[dict] = []
    client = FakeRedisStream(
        [
            ("1-0", {"event": json.dumps(knowledge_event())}),
            ("2-0", {"event": "{not json"}),
            ("3-0", {}),
        ]
    )
    worker = consumer(client, lambda payload: seen.append(payload) is None)

    handled = worker.run_once()

    assert handled == 1
    assert [payload["event_id"] for payload in seen] == ["event-1"]
    assert client.acked == ["1-0", "2-0", "3-0"]


def test_consumer_leaves_retryable_entries_pending() -> None:
    client = FakeRedisStream([("1-0", {"event": json.dumps(knowledge_event())})])
    worker = consumer(client, lambda payload: False)

    assert worker.run_once() == 0
    assert client.acked == []
