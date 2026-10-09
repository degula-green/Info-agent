from __future__ import annotations

import unittest

from app.application.callback_service import CallbackLane
from app.application.index_service import MVPIndexService
from app.application.memory_service import MemoryCandidateService
from app.application.parse_service import MVPParseService
from app.application.runtime import MVPWorkerRuntime, _event_payload_from_job
from app.config import settings
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


def test_event_payload_from_job_prefers_full_source_payload() -> None:
    payload = {
        "resource_type": "message",
        "resource_id": RESOURCE_ID,
        "knowledge_item_id": KNOWLEDGE_ITEM,
        "source_conversation_name": "aims",
        "source_platform": "feishu",
        "sender_platform": "feishu",
    }

    assert _event_payload_from_job({"source_payload": payload}) == payload


class _ArtifactStore:
    def download_source(self, context, path):
        raise AssertionError("message processing must not download an attachment")


class _Indexer:
    def __init__(self):
        self.chunks: list[Chunk] = []
        self.deleted_resources: list[str] = []

    def delete_older_versions(self, **kwargs):
        return 0

    def index_chunks(self, chunks):
        self.chunks.extend(chunks)
        return len(chunks)

    def delete_resource(self, *, resource_id: str) -> int:
        self.deleted_resources.append(resource_id)
        return 1


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
        claimed = repository.claim_jobs("parse", limit=1, job_id=job["id"])[0]
        runtime._run_parse(claimed)
        job = repository.get_job(job["id"])
        claimed = repository.claim_jobs("index", limit=1, job_id=job["id"])[0]
        runtime._run_index(claimed)
        job = repository.get_job(job["id"])
        self.assertEqual(job["status"], "ready")
        claimed = repository.claim_jobs("memory", limit=1, job_id=job["id"])[0]
        # The regex source is off by default since Phase 2 moved candidate
        # discovery to the window scan; this covers the transitional switch.
        original = settings.memory_regex_candidates_enabled
        object.__setattr__(settings, "memory_regex_candidates_enabled", True)
        try:
            runtime._run_memory(claimed)
        finally:
            object.__setattr__(settings, "memory_regex_candidates_enabled", original)
        self.assertGreaterEqual(len(repository.candidates), 1)
        self.assertTrue(indexer.chunks)
        self.assertTrue(all(chunk.embedding_status == "ready" for chunk in indexer.chunks))
        self.assertTrue(any(event["event_type"] == "knowledge.rag.ready" for event in repository.outbox))
        self.assertTrue(publisher.payloads)

    def test_dispatch_deletion_emits_knowledge_callback(self) -> None:
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
            "event_id": "00000000-0000-0000-0000-000000000011",
            "event_type": "knowledge.deletion.requested",
            "schema_version": 1,
            "occurred_at": "2026-10-06T00:00:00Z",
            "trace_id": "00000000-0000-0000-0000-000000000012",
            "organization_id": SCOPE_ID,
            "producer": "module-2",
            "payload": {
                "deletion_request_id": "00000000-0000-0000-0000-000000000012",
                "deletion_target_id": "00000000-0000-0000-0000-000000000013",
                "knowledge_item_id": KNOWLEDGE_ITEM,
                "resource_type": "message",
                "resource_id": RESOURCE_ID,
                "content_version": 1,
                "acl_version": 1,
                "scope_type": "organization",
                "scope_id": SCOPE_ID,
            },
        })
        self.assertEqual(indexer.deleted_resources, [RESOURCE_ID])
        self.assertTrue(any(payload.get("status") == "deleted" for payload in publisher.payloads))


if __name__ == "__main__":
    unittest.main()
