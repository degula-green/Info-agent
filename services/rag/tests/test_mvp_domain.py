from __future__ import annotations

import unittest

from app.domain.rag import Chunk, ResourceContext, normalized_text, time_bucket


def context() -> ResourceContext:
    return ResourceContext.from_event_and_source(
        {
            "resource_type": "message",
            "resource_id": "00000000-0000-0000-0000-000000000001",
            "knowledge_item_id": "00000000-0000-0000-0000-000000000002",
            "source_audience_policy": "organization_members",
            "content_version": 1,
            "acl_version": 2,
            "content_hash": "0" * 64,
            "content_access_required": False,
        },
        {
            "scope_type": "organization",
            "scope_id": "00000000-0000-0000-0000-000000000003",
            "knowledge_base_id": "00000000-0000-0000-0000-000000000004",
            "content_hash": "0" * 64,
        },
    )


class DomainTests(unittest.TestCase):
    def test_chunk_id_is_deterministic_and_variant_aware(self) -> None:
        first = Chunk.create(
            context=context(),
            snapshot_id="snapshot",
            chunk_index=0,
            chunk_count=1,
            content="hello",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        second = Chunk.create(
            context=context(),
            snapshot_id="snapshot",
            chunk_index=0,
            chunk_count=1,
            content="hello",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        protected = Chunk.create(
            context=context(),
            snapshot_id="snapshot",
            chunk_index=0,
            chunk_count=1,
            content="hello",
            variant="protected",
            processing_version="v1",
            chunking_version="v1",
        )
        self.assertEqual(first.chunk_id, second.chunk_id)
        self.assertNotEqual(first.chunk_id, protected.chunk_id)
        self.assertEqual(first.logical_position_key, protected.logical_position_key)

    def test_normalization_and_time_bucket(self) -> None:
        self.assertEqual(normalized_text(" A－B  C "), "abc")
        self.assertEqual(time_bucket("2026-09-27T10:00:00Z"), "2026-09")


if __name__ == "__main__":
    unittest.main()
