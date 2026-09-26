from __future__ import annotations

import unittest

from app.application.callback_service import CallbackLane
from app.application.index_service import MVPIndexService
from app.application.memory_service import MemoryCandidateService
from app.application.parse_service import MVPParseService
from app.application.runtime import MVPWorkerRuntime
from app.domain.rag import Chunk
from app.infrastructure.embedding.client import HashEmbeddingProvider
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


KNOWLEDGE_ITEM = "00000000-0000-0000-0000-000000000002"
RESOURCE_ID = "00000000-0000-0000-0000-000000000001"
SCOPE_ID = "00000000-0000-0000-0000-000000000003"
KB_ID = "00000000-0000-0000-0000-000000000004"


class _Knowledge:
    def get_knowledge(self, knowledge_item_id, **kwargs):
        return {
            "knowledge_item_id": knowledge_item_id,
            "resource_type": "message",
            "resource_id": RESOURCE_ID,
            "knowledge_base_id": KB_ID,
            "scope_type": "organization",
            "scope_id": SCOPE_ID,
            "content_version": 1,
            "acl_version": 1,
            "content_hash": "a" * 64,
            "content_access_required": False,
            "lifecycle_status": "active",
        }

    def get_content(self, knowledge_item_id, **kwargs):
        return {
            "knowledge_item_id": knowledge_item_id,
            "content_version": 1,
            "content_variant": kwargs.get("content_variant") or "display",
            "content_hash": "a" * 64,
            "text": "青云项目已进入交付阶段，张三负责延期处理。",
        }


class _ArtifactStore:
    def download_source(self, context, path):
        raise AssertionError("message processing must not download an attachment")


class _Indexer:
    def __init__(self):
        self.chunks: list[Chunk] = []

    def delete_older_versions(self, **kwargs):
        return 0

    def index_chunks(self, chunks):
        self.chunks.extend(chunks)
        return len(chunks)


class _Publisher:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)


class RuntimeTests(unittest.TestCase):
    def test_dispatch_parse_index_memory_callback(self) -> None:
        repository = InMemoryRagMVPRepository()
        indexer = _Indexer()
        publisher = _Publisher()
        callback = CallbackLane(repository=repository, publisher=publisher)
        runtime = MVPWorkerRuntime(
            repository=repository,
            parse_service=MVPParseService(
                knowledge=_Knowledge(),
                artifact_store=_ArtifactStore(),
            ),
            index_service=MVPIndexService(
                repository=repository,
                indexer=indexer,
                embedding=HashEmbeddingProvider(dimensions=1536),
            ),
            memory_service=MemoryCandidateService(repository=repository),
            callback_lane=callback,
        )
        runtime.handle({
            "event_id": "00000000-0000-0000-0000-000000000010",
            "event_type": "knowledge.ready",
            "schema_version": 1,
            "occurred_at": "2026-09-27T00:00:00Z",
            "trace_id": "trace",
            "organization_id": SCOPE_ID,
            "producer": "module-2",
            "payload": {
                "resource_type": "message",
                "resource_id": RESOURCE_ID,
                "knowledge_item_id": KNOWLEDGE_ITEM,
                "source_audience_policy": "organization_members",
                "content_version": 1,
                "acl_version": 1,
                "content_hash": "a" * 64,
                "content_access_required": False,
            },
        })
        job = repository.get_job(source_event_id="00000000-0000-0000-0000-000000000010")
        self.assertIsNotNone(job)
        runtime._run_parse(job)
        job = repository.get_job(job["id"])
        runtime._run_index(job)
        job = repository.get_job(job["id"])
        self.assertEqual(job["status"], "ready")
        runtime._run_memory(job)
        self.assertGreaterEqual(len(repository.candidates), 1)
        self.assertTrue(indexer.chunks)
        self.assertTrue(all(chunk.embedding_status == "ready" for chunk in indexer.chunks))
        self.assertTrue(any(event["event_type"] == "knowledge.rag.ready" for event in repository.outbox))
        self.assertTrue(publisher.payloads)


if __name__ == "__main__":
    unittest.main()
