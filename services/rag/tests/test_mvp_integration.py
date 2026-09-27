from __future__ import annotations

import os
import unittest
import uuid

from app.application.index_service import MVPIndexService
from app.application.entity_review_service import EntityReviewService
from app.application.parse_service import MVPParseService
from app.application.qa_service import QAService
from app.application.rag_service import RAGRetrievalService, RetrievalResponse
from app.application.runtime import MVPWorkerRuntime
from app.domain.rag import Chunk, ResourceContext, SearchRequest, SearchResult
from app.infrastructure.embedding.client import HashEmbeddingProvider
from app.infrastructure.persistence.mvp import PostgresRagMVPRepository
from app.infrastructure.rag_elasticsearch import RagChunkIndex
from app.infrastructure.service1.rag_authorization import AllowAllAuthorizationGateway


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
            claimed = repository.claim_jobs(
                "parse",
                limit=1,
                job_id=job["id"],
            )[0]
            runtime._run_parse(claimed)
            job = repository.get_job(job["id"])
            claimed = repository.claim_jobs(
                "index",
                limit=1,
                job_id=job["id"],
            )[0]
            runtime._run_index(claimed)
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

    def test_postgres_qa_history_and_entity_merge(self) -> None:
        repository = PostgresRagMVPRepository()
        user_id = str(uuid.uuid4())
        scope_id = str(uuid.uuid4())
        candidate_id = None
        try:
            qa = QAService(
                repository=repository,
                retrieval_service=_FixedRetrieval(),
                answer_provider=_FixedAnswerProvider(),
            )
            answer = qa.answer(
                SearchRequest(
                    query="integration question",
                    user_id=user_id,
                    scope_type="organization",
                    scope_id=scope_id,
                )
            )
            conversation = repository.get_qa_conversation(
                user_id=user_id,
                conversation_id=answer["conversation_id"],
            )
            self.assertIsNotNone(conversation)
            self.assertEqual(
                [item["role"] for item in conversation["messages"]],
                ["user", "assistant"],
            )
            stream_events = list(
                qa.answer_stream(
                    SearchRequest(
                        query="stream integration question",
                        user_id=user_id,
                        scope_type="organization",
                        scope_id=scope_id,
                    )
                )
            )
            stream_conversation_id = stream_events[0][1]["conversation_id"]
            stream_conversation = repository.get_qa_conversation(
                user_id=user_id,
                conversation_id=stream_conversation_id,
            )
            self.assertEqual(stream_events[-1][0], "done")
            self.assertEqual(
                [item["role"] for item in stream_conversation["messages"]],
                ["user", "assistant"],
            )
            self.assertEqual(
                stream_conversation["messages"][-1]["status"],
                "completed",
            )

            context = ResourceContext.from_event_and_source(
                {
                    "resource_type": "message",
                    "resource_id": str(uuid.uuid4()),
                    "knowledge_item_id": str(uuid.uuid4()),
                    "source_audience_policy": "organization_members",
                    "content_version": 1,
                    "acl_version": 1,
                    "content_hash": "b" * 64,
                    "content_access_required": False,
                },
                {
                    "scope_type": "organization",
                    "scope_id": scope_id,
                    "knowledge_base_id": str(uuid.uuid4()),
                    "content_hash": "b" * 64,
                },
            )
            snapshot_id = repository.upsert_snapshot(context)
            chunk = Chunk.create(
                context=context,
                snapshot_id=snapshot_id,
                chunk_index=0,
                chunk_count=1,
                content="integration candidate",
                variant="display",
                processing_version="v1",
                chunking_version="v1",
            )
            repository.upsert_chunks([chunk])
            candidate_id = repository.upsert_candidate_mention(
                scope_type="organization",
                scope_id=scope_id,
                candidate_name="Integration Candidate",
                normalized_key="integration candidate",
                domain="project",
                chunk=chunk,
                context_excerpt=chunk.content,
            )
            entity = repository.upsert_entity(
                scope_type="organization",
                scope_id=scope_id,
                domain="project",
                canonical_name="Integration Target",
                normalized_key="integration target",
            )
            result = EntityReviewService(repository=repository).review(
                scope_type="organization",
                scope_id=scope_id,
                candidate_id=candidate_id,
                reviewer_id=str(uuid.uuid4()),
                review_request_id=str(uuid.uuid4()),
                idempotency_key=None,
                action="merge",
                expected_status="new",
                canonical_name=None,
                domain=None,
                target_entity_id=entity["id"],
                note=None,
            )
            self.assertEqual(result["status"], "merged")
            with repository._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT COUNT(*) FROM rag_mvp.entity_aliases
                           WHERE entity_id=%s::uuid AND normalized_alias='integration candidate'""",
                        (entity["id"],),
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 1)
        finally:
            with repository._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM rag_mvp.qa_conversations WHERE user_id=%s::uuid",
                        (user_id,),
                    )
                    if candidate_id:
                        cursor.execute(
                            "DELETE FROM rag_mvp.entity_candidates WHERE id=%s::uuid",
                            (candidate_id,),
                        )
                    cursor.execute(
                        "DELETE FROM rag_mvp.entity_registry WHERE scope_type='organization' AND scope_id=%s::uuid",
                        (scope_id,),
                    )
                    cursor.execute(
                        "DELETE FROM rag_mvp.resource_snapshots WHERE scope_id=%s::uuid",
                        (scope_id,),
                    )

    def test_postgres_lease_epoch_blocks_stale_worker(self) -> None:
        repository = PostgresRagMVPRepository()
        event_id = str(uuid.uuid4())
        item_id = str(uuid.uuid4())
        organization_id = str(uuid.uuid4())
        try:
            job = repository.create_or_get_job(
                {
                    "event_id": event_id,
                    "event_type": "knowledge.ready",
                    "schema_version": 1,
                    "occurred_at": "2026-09-27T00:00:00Z",
                    "trace_id": "lease-integration",
                    "organization_id": organization_id,
                    "producer": "module-2",
                    "payload": {
                        "resource_type": "message",
                        "resource_id": str(uuid.uuid4()),
                        "knowledge_item_id": item_id,
                        "scope_type": "organization",
                        "scope_id": organization_id,
                        "organization_id": organization_id,
                        "source_audience_policy": "organization_members",
                        "content_version": 1,
                        "acl_version": 1,
                        "content_hash": "f" * 64,
                        "content_access_required": False,
                    },
                }
            )
            first = repository.claim_jobs(
                "parse",
                limit=1,
                job_id=job["id"],
            )[0]
            with repository._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE rag_mvp.processing_jobs
                           SET lease_until=CURRENT_TIMESTAMP - INTERVAL '1 second'
                           WHERE id=%s::uuid""",
                        (job["id"],),
                    )
            second = repository.claim_jobs(
                "parse",
                limit=1,
                job_id=job["id"],
            )[0]
            self.assertGreater(second["lease_epoch"], first["lease_epoch"])
            self.assertFalse(
                repository.update_job_if_owned(
                    job["id"],
                    owner=first["lease_owner"],
                    epoch=first["lease_epoch"],
                    fields={"status": "failed"},
                )
            )
        finally:
            with repository._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM rag_mvp.processing_jobs WHERE knowledge_item_id=%s::uuid",
                        (item_id,),
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


class _FixedRetrieval:
    def search(self, request):
        return RetrievalResponse(
            request_id="integration-qa",
            results=[
                SearchResult(
                    chunk_id="integration-chunk",
                    content="integration answer",
                    score=1.0,
                    rank=1,
                    source={
                        "knowledge_item_id": str(uuid.uuid4()),
                        "resource_type": "message",
                        "resource_id": str(uuid.uuid4()),
                        "content_variant": "display",
                    },
                )
            ],
            diagnostics={"effective_execution_path": "traditional"},
        )


class _FixedAnswerProvider:
    def generate(self, question, results):
        return "integration answer"

    def generate_stream(self, question, results):
        yield "integration "
        yield "answer"


if __name__ == "__main__":
    unittest.main()
