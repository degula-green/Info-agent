from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.ports import ProcessingOutput
from app.application.worker import RAGEventHandler
from app.domain.models import ParsedDocument
from app.infrastructure.events.redis_streams import RedisStreamPublisher, RedisStreamWorker
from app.infrastructure.events import redis_streams
from app.infrastructure.module2.knowledge_client import Module2KnowledgeClient
from app.infrastructure.persistence.repository import InMemoryRagRepository


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
        worker = RedisStreamWorker.__new__(RedisStreamWorker)
        worker.client, worker.handler = client, lambda _event: None
        worker.stream, worker.group, worker.consumer = "knowledge:ready", "rag-workers", "test"
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(client.acked, [("knowledge:ready", "rag-workers", b"3-0")])


if __name__ == "__main__":
    unittest.main()
