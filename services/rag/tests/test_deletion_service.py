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


if __name__ == "__main__":
    unittest.main()
