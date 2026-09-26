from __future__ import annotations

import os
import unittest
import uuid

from app.application.index_service import MVPIndexService
from app.application.parse_service import MVPParseService
from app.application.rag_service import RAGRetrievalService
from app.application.runtime import MVPWorkerRuntime
from app.infrastructure.embedding.client import HashEmbeddingProvider
from app.infrastructure.persistence.mvp import PostgresRagMVPRepository
from app.infrastructure.rag_elasticsearch import RagChunkIndex
from app.infrastructure.service1.rag_authorization import AllowAllAuthorizationGateway
from app.domain.rag import SearchRequest


@unittest.skipUnless(os.getenv("RAG_MVP_INTEGRATION") == "1", "external integration disabled")
class MVPEndToEndIntegrationTests(unittest.TestCase):
    def test_postgres_and_elasticsearch_round_trip(self) -> None:
        repository = PostgresRagMVPRepository()
        index = RagChunkIndex()
        knowledge = _Knowledge()
        runtime = MVPWorkerRuntime(
            repository=repository,
            parse_service=MVPParseService(
                knowledge=knowledge,
                artifact_store=_ArtifactStore(),
            ),
            index_service=MVPIndexService(
                repository=repository,
                indexer=index,
                embedding=HashEmbeddingProvider(dimensions=1536),
            ),
            memory_service=_Memory(),
            callback_lane=_Callback(repository),
        )
        identifiers = {name: str(uuid.uuid4()) for name in (
            "event", "resource", "item", "scope", "kb",
        )}
        event = {
            "event_id": identifiers["event"],
            "event_type": "knowledge.ready",
            "schema_version": 1,
            "occurred_at": "2026-09-27T00:00:00Z",
            "trace_id": "integration",
            "organization_id": identifiers["scope"],
            "producer": "module-2",
            "payload": {
                "resource_type": "message",
                "resource_id": identifiers["resource"],
                "knowledge_item_id": identifiers["item"],
                "source_audience_policy": "organization_members",
                "content_version": 1,
                "acl_version": 1,
                "content_hash": "a" * 64,
                "content_access_required": False,
            },
        }
        knowledge.identifiers = identifiers
        try:
            repository.upsert_entity(
                scope_type="organization",
                scope_id=identifiers["scope"],
                domain="project",
                canonical_name="青云项目",
                normalized_key="青云项目",
            )
            runtime.handle(event)
            job = repository.get_job(source_event_id=identifiers["event"])
            runtime._run_parse(job)
            job = repository.get_job(job["id"])
            runtime._run_index(job)
            chunks = repository.list_chunks(scope_type="organization", scope_id=identifiers["scope"])
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].embedding_status, "ready")
            self.assertTrue(chunks[0].branch_keys)
            service = RAGRetrievalService(
                repository=repository,
                indexer=index,
                embedding=HashEmbeddingProvider(dimensions=1536),
                authorization=AllowAllAuthorizationGateway(),
            )
            response = service.search(SearchRequest(
                query="integration marker",
                user_id="integration-user",
                scope_type="organization",
                scope_id=identifiers["scope"],
                knowledge_base_ids=(identifiers["kb"],),
            ))
            self.assertGreaterEqual(len(response.results), 1)
        finally:
            index.client.delete_by_query(
                index="rag_chunks_display_write",
                query={"term": {"knowledge_item_id": identifiers["item"]}},
                conflicts="proceed",
                refresh=True,
            )
            with repository._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM rag_mvp.processing_jobs WHERE knowledge_item_id=%s::uuid",
                        (identifiers["item"],),
                    )
                    cursor.execute(
                        "DELETE FROM rag_mvp.resource_snapshots WHERE knowledge_item_id=%s::uuid",
                        (identifiers["item"],),
                    )
                    cursor.execute(
                        "DELETE FROM rag_mvp.entity_registry WHERE scope_type='organization' AND scope_id=%s::uuid",
                        (identifiers["scope"],),
                    )


class _Knowledge:
    identifiers = {}

    def get_knowledge(self, knowledge_item_id, **kwargs):
        ids = self.identifiers
        return {
            "knowledge_item_id": ids["item"],
            "resource_type": "message",
            "resource_id": ids["resource"],
            "knowledge_base_id": ids["kb"],
            "scope_type": "organization",
            "scope_id": ids["scope"],
            "content_version": 1,
            "acl_version": 1,
            "content_hash": "a" * 64,
            "content_access_required": False,
            "lifecycle_status": "active",
        }

    def get_content(self, knowledge_item_id, **kwargs):
        return {
            "knowledge_item_id": self.identifiers["item"],
            "content_version": 1,
            "content_variant": "display",
            "content_hash": "a" * 64,
            "text": "integration marker 青云项目已进入交付阶段",
        }


class _ArtifactStore:
    def download_source(self, context, path):
        raise AssertionError("message processing must not download an attachment")


class _Memory:
    def process(self, job):
        return {"candidate_count": 0, "mention_count": 0}


class _Callback:
    def __init__(self, repository):
        self.repository = repository

    def flush(self, *, limit=50):
        return 0


if __name__ == "__main__":
    unittest.main()
