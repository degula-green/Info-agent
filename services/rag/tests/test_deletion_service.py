from __future__ import annotations

import unittest

from app.application.deletion_service import DeletionService
from app.domain.rag import Chunk, ResourceContext


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


class _Indexer:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_resource(self, *, resource_id: str) -> int:
        self.deleted.append(resource_id)
        return 2


class DeletionServiceTests(unittest.TestCase):
    def test_delete_event_removes_vectors_and_chunks(self) -> None:
        from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

        repository = InMemoryRagMVPRepository()
        context = _context()
        snapshot_id = repository.upsert_snapshot(context)
        chunk = Chunk.create(
            context=context,
            snapshot_id=snapshot_id,
            chunk_index=0,
            chunk_count=1,
            content="delete target",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        repository.upsert_chunks([chunk])
        indexer = _Indexer()
        service = DeletionService(repository=repository, indexer=indexer)
        result = service.handle({
            "payload": {
                "knowledge_item_id": context.knowledge_item_id,
                "resource_id": context.resource_id,
            },
        })
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["deleted_vectors"], 2)
        self.assertEqual(result["deleted_chunks"], 1)
        self.assertEqual(indexer.deleted, [context.resource_id])
        self.assertEqual(repository.list_chunks(), [])


    def test_delete_event_emits_deleted_callback(self) -> None:
        from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository
        from app.application.callback_service import CallbackLane

        repository = InMemoryRagMVPRepository()
        published: list[dict] = []

        class _Publisher:
            def send(self, payload: dict) -> None:
                published.append(payload)

        callback = CallbackLane(repository=repository, publisher=_Publisher())
        indexer = _Indexer()
        service = DeletionService(repository=repository, indexer=indexer, callback_lane=callback)
        result = service.handle({
            "event_id": "00000000-0000-0000-0000-000000000099",
            "payload": {
                "knowledge_item_id": "00000000-0000-0000-0000-000000000003",
                "resource_id": "00000000-0000-0000-0000-000000000004",
                "deletion_request_id": "00000000-0000-0000-0000-000000000010",
                "deletion_target_id": "00000000-0000-0000-0000-000000000011",
                "content_version": 1,
                "acl_version": 1,
                "scope_type": "organization",
                "scope_id": "00000000-0000-0000-0000-000000000001",
            },
        })
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["status"], "deleted")
        self.assertEqual(published[0]["knowledge_item_id"], "00000000-0000-0000-0000-000000000003")

    def test_deleted_lifecycle_result_is_not_authorized(self) -> None:
        from app.application.rag_service import RAGRetrievalService
        from app.domain.rag import SearchRequest, SearchResult

        class _Repository:
            pass

        class _Indexer:
            pass

        class _Authorization:
            def check_batch(self, **kwargs):
                return [True for _ in kwargs.get("checks", [])]

        request = SearchRequest(
            user_id="00000000-0000-0000-0000-000000000001",
            scope_type="organization",
            scope_id="00000000-0000-0000-0000-000000000002",
            query="hello",
        )
        scope = type("_Scope", (), {"snapshot_id": None})()
        active = SearchResult(chunk_id="a", content="active", score=1.0, source={"knowledge_item_id": "item-a", "resource_type": "message", "resource_id": "msg-a", "lifecycle_status": "active"})
        deleted = SearchResult(chunk_id="d", content="deleted", score=1.0, source={"knowledge_item_id": "item-d", "resource_type": "message", "resource_id": "msg-d", "lifecycle_status": "deleted"})
        service = RAGRetrievalService(repository=_Repository(), indexer=_Indexer(), embedding=object(), authorization=_Authorization())
        allowed = service._authorize_results(request, [active, deleted], scope)
        self.assertEqual([item.chunk_id for item in allowed], ["a"])

if __name__ == "__main__":
    unittest.main()
