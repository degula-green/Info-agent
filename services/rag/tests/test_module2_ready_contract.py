from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from app.application.ports import ProcessingOutput
from app.application.worker import RAGEventHandler
from app.domain.models import ParsedDocument
from app.infrastructure.events.redis_streams import RedisStreamPublisher, RedisStreamWorker
from app.infrastructure.events import redis_streams
from app.infrastructure.module2.knowledge_client import Module2KnowledgeClient
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository


def ready_event(*, event_id: str = "00000000-0000-0000-0000-000000000001", protected: bool = False) -> dict:
    return {
        "event_id": event_id,
        "event_type": "knowledge.ready",
        "schema_version": 1,
        "occurred_at": "2026-09-16T08:00:00Z",
        "trace_id": "trace-1",
        "organization_id": "00000000-0000-0000-0000-000000000010",
        "producer": "module-2",
        "payload": {
            "resource_type": "knowledge_item",
            "resource_id": "00000000-0000-0000-0000-000000000020",
            "knowledge_item_id": "00000000-0000-0000-0000-000000000020",
            "source_message_id": "00000000-0000-0000-0000-000000000030",
            "source_attachment_id": "00000000-0000-0000-0000-000000000040",
            "content_version": 2,
            "acl_version": 3,
            "content_variant": "display",
            "content_access_required": protected,
        },
    }


class _Response:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class _Http:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response({"ok": True})


class _Knowledge:
    def __init__(self, *, protected: bool):
        self.protected = protected
        self.calls = []

    def get_knowledge(self, knowledge_item_id, **versions):
        self.calls.append(("knowledge", knowledge_item_id, versions))
        return {
            "knowledge_item_id": knowledge_item_id,
            "organization_id": "00000000-0000-0000-0000-000000000010",
            "knowledge_base_id": "00000000-0000-0000-0000-000000000050",
            "content_version": 2,
            "acl_version": 3,
            "attachments": [{
                "attachment_id": "00000000-0000-0000-0000-000000000040",
                "file_name": "secret.txt" if self.protected else "report.txt",
                "mime_type": "text/plain",
                "content_access_required": self.protected,
            }],
        }

    def get_attachment(self, attachment_id, **versions):
        self.calls.append(("attachment", attachment_id, versions))
        return {
            "id": attachment_id,
            "file_name": "report.txt",
            "mime_type": "text/plain",
            "object_ref": "objects/report.txt",
            "content_access_required": False,
        }

    def get_content(self, knowledge_item_id, **versions):
        self.calls.append(("content", knowledge_item_id, versions))
        return {"text": "message"}


class _Preprocessor:
    def __init__(self):
        self.calls = []

    def process(self, item, *, vectorize=True):
        self.calls.append(item)
        return ProcessingOutput(item.attachment, ParsedDocument("ok", [], "fixture", "1"), [])


class _Indexer:
    def __init__(self):
        self.chunks = []

    def index_chunks(self, chunks):
        self.chunks.extend(chunks)
        return len(chunks)


class _Redis:
    def __init__(self, envelope=None):
        self.envelope = envelope
        self.added = []
        self.acked = []

    def xadd(self, stream, fields, maxlen=None):
        self.added.append((stream, fields, maxlen))
        return b"1-0"

    def xgroup_create(self, *_args, **_kwargs):
        return True

    def xreadgroup(self, *_args, **_kwargs):
        if self.envelope is None:
            return []
        envelope, self.envelope = self.envelope, None
        return [(b"knowledge:ready", [(b"1-0", {b"event": json.dumps(envelope).encode()})])]

    def xautoclaim(self, *_args, **_kwargs):
        return (b"0-0", [])

    def xack(self, stream, group, message_id):
        self.acked.append((stream, group, message_id))


class Module2ReadyContractTests(unittest.TestCase):
    def test_sparse_attachment_event_hydrates_metadata_before_context_creation(self):
        knowledge = _Knowledge(protected=False)
        handler = RAGEventHandler(preprocessor=_Preprocessor(), indexer=_Indexer(), repository=InMemoryRagRepository(), knowledge=knowledge)
        context = handler._contexts({
            "knowledge_item_id": "ki-1", "attachment_id": "att-1",
            "content_version": 2, "acl_version": 3,
        })[0]
        self.assertEqual(context.file_name, "report.txt")
        self.assertEqual(context.object_ref, "objects/report.txt")
        self.assertEqual(context.knowledge_base_id, "00000000-0000-0000-0000-000000000050")
        self.assertEqual([call[0] for call in knowledge.calls], ["knowledge", "attachment"])

    def test_complete_attachment_event_uses_authoritative_knowledge_ownership(self):
        knowledge = _Knowledge(protected=False)
        handler = RAGEventHandler(preprocessor=_Preprocessor(), indexer=_Indexer(), repository=InMemoryRagRepository(), knowledge=knowledge)
        context = handler._contexts({
            "knowledge_item_id": "00000000-0000-0000-0000-000000000020",
            "attachment_id": "00000000-0000-0000-0000-000000000040",
            "file_name": "event-name.txt",
            "mime_type": "text/plain",
            "object_ref": "objects/from-event.txt",
            "knowledge_base_id": "00000000-0000-0000-0000-000000000099",
            "content_version": 2,
            "acl_version": 3,
        })[0]
        self.assertEqual(context.file_name, "report.txt")
        self.assertEqual(context.knowledge_base_id, "00000000-0000-0000-0000-000000000050")
        self.assertEqual([call[0] for call in knowledge.calls], ["knowledge"])

    def test_missing_name_uses_stable_mime_fallback(self):
        knowledge = Mock()
        knowledge.get_attachment.return_value = {"id": "att-1", "mime_type": "application/pdf", "object_ref": "objects/report"}
        handler = RAGEventHandler(preprocessor=_Preprocessor(), indexer=_Indexer(), repository=InMemoryRagRepository(), knowledge=knowledge)
        context = handler._contexts({"attachment_id": "att-1", "content_version": 2, "acl_version": 3})[0]
        self.assertEqual(context.file_name, "attachment-att-1.pdf")

    def test_redis_connection_uses_url_scheme_for_tls(self):
        factory = Mock(return_value=object())
        redis_module = SimpleNamespace(Redis=SimpleNamespace(from_url=factory))
        base_settings = {
            "redis_database": 1,
            "redis_username": "",
            "redis_password": "",
            "authz_connect_timeout_seconds": 0.2,
            "authz_timeout_seconds": 0.8,
            "redis_block_ms": 5000,
        }

        for tls, expected_url in (
            (False, "redis://cache.example:6379/1"),
            (True, "rediss://cache.example:6379/1"),
        ):
            factory.reset_mock()
            runtime_settings = SimpleNamespace(
                **base_settings,
                redis_url="redis://cache.example:6379/1",
                redis_tls=tls,
            )
            with patch.object(redis_streams, "settings", runtime_settings), patch.dict(sys.modules, {"redis": redis_module}):
                redis_streams._build_redis()
            args, kwargs = factory.call_args
            self.assertEqual(args[0], expected_url)
            self.assertNotIn("ssl", kwargs)
            self.assertEqual(kwargs["socket_timeout"], 6.0)

    def test_client_sends_content_and_acl_versions_on_every_source_call(self):
        http = _Http()
        client = Module2KnowledgeClient(base_url="http://knowledge", token="service-token", http=http)
        client.get_knowledge("ki-1", content_version=2, acl_version=3)
        client.get_content("ki-1", content_version=2, acl_version=3, content_variant="display")
        client.get_attachment("att-1", content_version=2, acl_version=3)
        self.assertEqual([call[0] for call in http.calls], ["GET", "GET", "GET"])
        self.assertTrue(all("content_version=2" in call[1] and "acl_version=3" in call[1] for call in http.calls))
        self.assertIn("content_variant=display", http.calls[1][1])
        self.assertTrue(all(call[2]["token"] == "service-token" for call in http.calls))
        self.assertTrue(all(call[2]["headers"]["X-Caller-Service"] == "rag" for call in http.calls))

    def test_unprotected_attachment_is_fetched_from_attachment_endpoint(self):
        knowledge = _Knowledge(protected=False)
        handler = RAGEventHandler(preprocessor=_Preprocessor(), indexer=_Indexer(), repository=InMemoryRagRepository(), knowledge=knowledge)
        contexts = handler._contexts(ready_event()["payload"], organization_id=ready_event()["organization_id"])
        self.assertEqual([call[0] for call in knowledge.calls], ["knowledge", "attachment"])
        self.assertEqual(contexts[0].object_ref, "objects/report.txt")
        self.assertEqual(contexts[0].content_version, 2)
        self.assertEqual(contexts[0].acl_version, 3)

    def test_protected_attachment_indexes_metadata_without_fetching_content(self):
        knowledge = _Knowledge(protected=True)
        preprocessor = _Preprocessor()
        indexer = _Indexer()
        repository = InMemoryRagRepository()
        handler = RAGEventHandler(preprocessor=preprocessor, indexer=indexer, repository=repository, knowledge=knowledge)
        handler.handle(ready_event(protected=True))
        self.assertEqual([call[0] for call in knowledge.calls], ["knowledge"])
        self.assertEqual(preprocessor.calls, [])
        self.assertEqual(len(indexer.chunks), 1)
        self.assertEqual(indexer.chunks[0].part_kind, "attachment_metadata")
        self.assertFalse(indexer.chunks[0].rag_eligible)
        self.assertEqual(next(iter(repository.processing_jobs.values()))["status"], "succeeded")

    def test_redis_stream_uses_event_field_and_only_acks_success(self):
        event = ready_event()
        publisher_client = _Redis()
        self.assertEqual(RedisStreamPublisher(client=publisher_client, stream="knowledge:ready").publish(event), "1-0")
        self.assertEqual(set(publisher_client.added[0][1]), {"event"})

        success_client = _Redis(event)
        worker = RedisStreamWorker.__new__(RedisStreamWorker)
        worker.client, worker.handler = success_client, lambda _event: None
        worker.stream, worker.group, worker.consumer = "knowledge:ready", "rag-workers", "test"
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(len(success_client.acked), 1)

        failed_client = _Redis(event)
        worker.client = failed_client
        worker.handler = lambda _event: (_ for _ in ()).throw(RuntimeError("failed"))
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(failed_client.acked, [])

    def test_redis_stream_acks_legacy_payload_entries(self):
        class LegacyRedis(_Redis):
            def xreadgroup(self, *_args, **_kwargs):
                return [(b"knowledge:ready", [(b"2-0", {b"payload": b"{}"})])]

        client = LegacyRedis()
        worker = RedisStreamWorker.__new__(RedisStreamWorker)
        worker.client, worker.handler = client, lambda _event: (_ for _ in ()).throw(AssertionError("legacy event was dispatched"))
        worker.stream, worker.group, worker.consumer = "knowledge:ready", "rag-workers", "test"
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(len(client.acked), 1)

    def test_redis_stream_processes_list_shaped_xautoclaim_response(self):
        event = ready_event()

        class ListClaimRedis(_Redis):
            def xautoclaim(self, *_args, **_kwargs):
                return [b"0-0", [(b"3-0", {b"event": json.dumps(event).encode()})], []]

            def xreadgroup(self, *_args, **_kwargs):
                return []

        client = ListClaimRedis()
        worker = _stream_worker(client, lambda _event: None)
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(client.acked, [("knowledge:ready", "rag-workers", b"3-0")])

    def test_redis_stream_creates_the_consumer_group_once_per_connection(self):
        """A failing XGROUP CREATE must not cost every iteration a round trip."""

        class CountingRedis(_Redis):
            def __init__(self):
                super().__init__()
                self.group_creates = 0

            def xgroup_create(self, *_args, **_kwargs):
                self.group_creates += 1
                return True

            def xreadgroup(self, *_args, **_kwargs):
                return []

        client = CountingRedis()
        worker = _stream_worker(client, lambda _event: None)
        worker.run_once()
        worker.run_once()
        worker.run_once()
        self.assertEqual(client.group_creates, 1)
        # Reconnect drops the connection the group was created on, so the next
        # iteration must create it again.
        worker._group_ready = False
        worker.run_once()
        self.assertEqual(client.group_creates, 2)

    def test_redis_stream_follows_the_xautoclaim_cursor_into_later_pages(self):
        """XAUTOCLAIM must be re-issued from `next_start_id` until it returns 0-0."""
        event = ready_event()

        class PagedRedis(_Redis):
            def __init__(self):
                super().__init__()
                self.starts = []
                self.pages = [
                    [b"4-0", [(b"2-0", {b"event": json.dumps(event).encode()})], []],
                    [b"5-0", [(b"3-0", {b"event": json.dumps(event).encode()})], []],
                    [b"0-0", [(b"4-0", {b"event": json.dumps(event).encode()})], []],
                ]

            def xautoclaim(self, _stream, _group, _consumer, *, start_id, **_kwargs):
                self.starts.append(start_id)
                return self.pages.pop(0)

            def xreadgroup(self, *_args, **_kwargs):
                return []

        client = PagedRedis()
        worker = _stream_worker(client, lambda _event: None)
        self.assertEqual(worker.run_once(), 3)
        self.assertEqual(client.starts, ["0-0", "4-0", "5-0"])
        self.assertEqual(
            sorted(client.acked),
            sorted(("knowledge:ready", "rag-workers", mid) for mid in (b"2-0", b"3-0", b"4-0")),
        )

    def test_redis_stream_dead_letters_an_entry_that_exhausted_its_deliveries(self):
        event = ready_event()
        client = _FailingRedis(event, times_delivered=3)
        worker = _stream_worker(client, lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
        with patch.object(redis_streams, "settings", _stream_settings()):
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(len(client.acked), 1)
        self.assertEqual(len(client.added), 1)
        dlq_stream, payload, maxlen = client.added[0]
        self.assertEqual(dlq_stream, "knowledge:ready.dlq")
        self.assertEqual(payload["dlq_source_message_id"], "2-0")
        self.assertIn("boom", payload["dlq_error"])
        # The original entry travels with the dead letter so it can be replayed.
        # The value stays bytes: the client is built with decode_responses=False.
        self.assertEqual(payload["event"], json.dumps(event).encode())

    def test_redis_stream_keeps_an_entry_pending_below_the_delivery_budget(self):
        event = ready_event()
        client = _FailingRedis(event, times_delivered=1)
        worker = _stream_worker(client, lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
        with patch.object(redis_streams, "settings", _stream_settings()):
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(client.acked, [])
        self.assertEqual(client.added, [])

    def test_redis_stream_keeps_an_entry_pending_when_delivery_count_is_unknown(self):
        """No XPENDING support must mean "retry", never "dead-letter on a guess"."""
        event = ready_event()
        client = _FailingRedis(event, times_delivered=None)
        worker = _stream_worker(client, lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
        with patch.object(redis_streams, "settings", _stream_settings()):
            self.assertEqual(worker.run_once(), 0)
        self.assertEqual(client.acked, [])
        self.assertEqual(client.added, [])

    def test_redis_stream_does_not_dead_letter_a_job_whose_ack_failed(self):
        """A successful job with a failed ACK must not be treated as a failure."""

        class AckFailingRedis(_FailingRedis):
            def __init__(self, event):
                super().__init__(event, times_delivered=9)

            def xack(self, stream, group, message_id):
                raise RuntimeError("connection reset")

        client = AckFailingRedis(ready_event())
        worker = _stream_worker(client, lambda _event: None)
        with patch.object(redis_streams, "settings", _stream_settings()):
            self.assertEqual(worker.run_once(), 1)
        self.assertEqual(client.added, [])
        self.assertEqual(client.acked, [])

    def test_private_knowledge_without_organization_creates_a_job(self):
        """Private knowledge has no organization and Knowledge sends "" for it.

        PostgreSQL rejects an empty string cast to uuid, and that exception used
        to escape `handle()` before the retry accounting, so every private-chat
        event failed forever with no processing_jobs row and no ACK.
        """
        cursor = _RecordingCursor()
        repository = PostgresRagRepository(connection_factory=lambda: _RecordingConnection(cursor))
        envelope = {**ready_event(), "organization_id": ""}

        job_id = repository.create_processing_job(envelope, job_type="full_process")
        self.assertIsNotNone(job_id)
        params = cursor.executed[0][1]
        self.assertEqual(params[3], None, "blank organization_id must reach %s::uuid as NULL")

        repository.add_outbox_event(
            {**ready_event(), "event_type": "knowledge.rag.processing", "organization_id": ""},
            aggregate_type="knowledge_item",
            aggregate_id="00000000-0000-0000-0000-000000000020",
        )
        outbox_params = cursor.executed[1][1]
        self.assertEqual(outbox_params[6], None)

    def test_redis_stream_logs_every_silently_dropped_entry(self):
        """An ACKed drop leaves no trace unless it is logged."""

        class PoisonRedis(_Redis):
            def xautoclaim(self, *_args, **_kwargs):
                return [b"0-0", [], []]

            def xreadgroup(self, *_args, **_kwargs):
                return [(b"knowledge:ready", [
                    (b"7-0", {b"event": b"{not json"}),
                    (b"8-0", {b"payload": b"{}"}),
                ])]

        client = PoisonRedis()
        worker = _stream_worker(client, lambda _event: (_ for _ in ()).throw(AssertionError("poison dispatched")))
        with self.assertLogs("rag.stream", level="WARNING") as captured:
            self.assertEqual(worker.run_once(), 2)
        logged = "\n".join(captured.output)
        self.assertIn("7-0", logged)
        self.assertIn("8-0", logged)
        self.assertEqual(len(client.acked), 2)


class _RecordingCursor:
    """Minimal psycopg cursor stand-in that records the SQL parameters."""

    def __init__(self):
        self.executed: list[tuple[str, Any]] = []
        self._row: tuple | None = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # create_processing_job validates RETURNING payload_hash against the one
        # it computed, so echo it back as the second column.
        self._row = ("29fc35d8-0000-0000-0000-000000000000", params[2])

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    # The repository's _connection context manager commits on success and
    # closes unconditionally.
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _stream_worker(client, handler):
    """Build a worker the way the existing stream tests do.

    ``__new__`` skips ``__init__``, so ``_group_ready`` is absent and
    ``_ensure_group_once`` has to tolerate that via ``getattr``.
    """
    worker = RedisStreamWorker.__new__(RedisStreamWorker)
    worker.client, worker.handler = client, handler
    worker.stream, worker.group, worker.consumer = "knowledge:ready", "rag-workers", "test"
    return worker


def _stream_settings(**overrides):
    values = {
        "redis_claim_idle_ms": 60000,
        "redis_batch_size": 10,
        "redis_block_ms": 0,
        "redis_max_retries": 3,
        "redis_claim_max_rounds": 8,
        "redis_dlq_maxlen": 10000,
        "redis_dlq_stream_name": "knowledge:ready.dlq",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FailingRedis(_Redis):
    """One entry that always fails handling, with a settable delivery count."""

    def __init__(self, event, *, times_delivered):
        super().__init__()
        self.payload = {b"event": json.dumps(event).encode()}
        self.times_delivered = times_delivered
        self.delivered = False

    def xautoclaim(self, *_args, **_kwargs):
        return [b"0-0", [], []]

    def xreadgroup(self, *_args, **_kwargs):
        if self.delivered:
            return []
        self.delivered = True
        return [(b"knowledge:ready", [(b"2-0", self.payload)])]

    def xpending_range(self, *_args, **_kwargs):
        if self.times_delivered is None:
            raise RuntimeError("XPENDING unsupported")
        return [{"message_id": b"2-0", "times_delivered": self.times_delivered}]


if __name__ == "__main__":
    unittest.main()
