from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.application.index_service import IndexStageError, MVPIndexService
from app.config import settings
from app.domain.rag import Chunk, ResourceContext
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


def _context() -> ResourceContext:
    return ResourceContext.from_event_and_source(
        {
            "resource_type": "message",
            "resource_id": "00000000-0000-0000-0000-000000000001",
            "knowledge_item_id": "00000000-0000-0000-0000-000000000002",
            "source_audience_policy": "organization_members",
            "content_version": 1,
            "acl_version": 1,
            "content_hash": "a" * 64,
            "content_access_required": False,
        },
        {
            "scope_type": "organization",
            "scope_id": "00000000-0000-0000-0000-000000000003",
            "knowledge_base_id": "00000000-0000-0000-0000-000000000004",
            "content_hash": "a" * 64,
        },
    )


class _Embedding:
    model = "test-model"
    dimensions = settings.embedding_dims

    def __init__(self, *, fail: bool = False, failures: int = 0) -> None:
        self.fail = fail
        self.failures = failures

    def embed(self, texts):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("embedding failed")
        if self.fail:
            raise RuntimeError("embedding failed")
        return [[0.1] * settings.embedding_dims for _ in texts]


class _Indexer:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0
        self.deleted_versions = []

    def index_chunks(self, chunks):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("es failed")
        return len(chunks)

    def delete_older_versions(self, *, resource_id, content_version):
        self.deleted_versions.append((resource_id, content_version))
        return 0


def _job(context: ResourceContext) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000010",
        "knowledge_item_id": context.knowledge_item_id,
        "resource_id": context.resource_id,
        "content_version": context.content_version,
        "scope_type": context.scope_type,
        "scope_id": context.scope_id,
        "resource_type": context.resource_type,
        "parse_status": "parsed",
    }


class ProjectionRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryRagMVPRepository()
        self.context = _context()
        snapshot_id = self.repository.upsert_snapshot(self.context)
        self.chunk = Chunk.create(
            context=self.context,
            snapshot_id=snapshot_id,
            chunk_index=0,
            chunk_count=1,
            content="hello",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        self.repository.upsert_chunks([self.chunk])

    def test_projection_exists_before_first_es_write(self) -> None:
        indexer = _Indexer()
        service = MVPIndexService(
            repository=self.repository,
            indexer=indexer,
            embedding=_Embedding(),
        )
        result = service.process(_job(self.context))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(indexer.calls, 1)
        projections = self.repository.list_projection_records(
            knowledge_item_id=self.context.knowledge_item_id,
            content_version=self.context.content_version,
        )
        self.assertEqual(len(projections), 1)
        self.assertEqual(projections[0]["status"], "ready")

    def test_es_failure_retries_then_succeeds(self) -> None:
        indexer = _Indexer(failures=1)
        service = MVPIndexService(
            repository=self.repository,
            indexer=indexer,
            embedding=_Embedding(),
        )
        with self.assertRaises(IndexStageError):
            service.process(_job(self.context))
        projection = self.repository.list_projection_records(
            knowledge_item_id=self.context.knowledge_item_id,
            content_version=self.context.content_version,
        )[0]
        self.assertEqual(projection["status"], "retry_wait")
        self.assertEqual(projection["failure_stage"], "indexing")

        self.repository.projections[0]["next_retry_at"] = datetime.now(timezone.utc)
        result = service.process(_job(self.context))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(indexer.calls, 2)

    def test_embedding_failure_is_retryable_and_never_metadata_only(self) -> None:
        service = MVPIndexService(
            repository=self.repository,
            indexer=_Indexer(),
            embedding=_Embedding(fail=True),
        )
        with self.assertRaises(IndexStageError):
            service.process(_job(self.context))
        projection = self.repository.list_projection_records(
            knowledge_item_id=self.context.knowledge_item_id,
            content_version=self.context.content_version,
        )[0]
        self.assertEqual(projection["status"], "retry_wait")
        self.assertEqual(projection["failure_stage"], "embedding")

    def test_embedding_retry_can_fail_more_than_once(self) -> None:
        indexer = _Indexer()
        service = MVPIndexService(
            repository=self.repository,
            indexer=indexer,
            embedding=_Embedding(failures=2),
        )

        for expected_retry_count in (1, 2):
            with self.assertRaises(IndexStageError):
                service.process(_job(self.context))
            projection = self.repository.list_projection_records(
                knowledge_item_id=self.context.knowledge_item_id,
                content_version=self.context.content_version,
            )[0]
            self.assertEqual(projection["status"], "retry_wait")
            self.assertEqual(projection["retry_count"], expected_retry_count)
            self.repository.projections[0]["next_retry_at"] = datetime.now(
                timezone.utc
            )

        result = service.process(_job(self.context))

        self.assertEqual(result["status"], "ready")
        projection = self.repository.list_projection_records(
            knowledge_item_id=self.context.knowledge_item_id,
            content_version=self.context.content_version,
        )[0]
        self.assertEqual(projection["status"], "ready")
        self.assertEqual(projection["retry_count"], 2)
        self.assertEqual(indexer.calls, 1)

    def test_es_retry_exhaustion_fails_projection_and_job_stage(self) -> None:
        indexer = _Indexer(failures=settings.index_max_retries)
        service = MVPIndexService(
            repository=self.repository,
            indexer=indexer,
            embedding=_Embedding(),
        )
        last_error: IndexStageError | None = None
        for _ in range(settings.index_max_retries):
            try:
                service.process(_job(self.context))
            except IndexStageError as exc:
                last_error = exc
            else:
                self.fail("projection retry exhaustion unexpectedly succeeded")
            if self.repository.projections:
                self.repository.projections[0]["next_retry_at"] = datetime.now(
                    timezone.utc
                )
        projection = self.repository.list_projection_records(
            knowledge_item_id=self.context.knowledge_item_id,
            content_version=self.context.content_version,
        )[0]
        self.assertIsNotNone(last_error)
        self.assertFalse(last_error.retryable)
        self.assertEqual(projection["status"], "failed")

    def test_metadata_only_requires_explicit_parse_status(self) -> None:
        repository = InMemoryRagMVPRepository()
        job = _job(self.context)
        service = MVPIndexService(
            repository=repository,
            indexer=_Indexer(),
            embedding=_Embedding(),
        )
        with self.assertRaisesRegex(IndexStageError, "did not produce chunks"):
            service.process(job)
        job["parse_status"] = "metadata_only"
        self.assertEqual(service.process(job)["status"], "metadata_only")


if __name__ == "__main__":
    unittest.main()
