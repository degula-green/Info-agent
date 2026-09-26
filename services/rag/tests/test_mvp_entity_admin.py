from __future__ import annotations

import unittest

from app.application.branch_refresh_service import BranchRefreshService
from app.domain.rag import Chunk, ResourceContext, normalized_text
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
            "sent_at": "2026-09-27T00:00:00Z",
        },
    )


class _Indexer:
    def __init__(self):
        self.updated = []

    def update_chunk_branches(self, chunks):
        self.updated.extend(chunks)
        return len(chunks)


class EntityAdminTests(unittest.TestCase):
    def test_candidate_review_promotes_and_refreshes_branch(self) -> None:
        repository = InMemoryRagMVPRepository()
        context = _context()
        snapshot_id = repository.upsert_snapshot(context)
        chunk = Chunk.create(
            context=context,
            snapshot_id=snapshot_id,
            chunk_index=0,
            chunk_count=1,
            content="青云官网项目将在九月上线",
            variant="display",
            processing_version="v1",
            chunking_version="v1",
        )
        chunk.sent_at = "2026-09-27T00:00:00Z"
        chunk.embedding_status = "ready"
        repository.upsert_chunks([chunk])
        candidate_id = repository.upsert_candidate_mention(
            scope_type="organization",
            scope_id=context.scope_id,
            candidate_name="青云官网项目",
            normalized_key=normalized_text("青云官网项目"),
            domain="project",
            chunk=chunk,
            context_excerpt=chunk.content,
        )
        result = repository.review_candidate(
            scope_type="organization",
            scope_id=context.scope_id,
            candidate_id=candidate_id,
            reviewer_id="00000000-0000-0000-0000-000000000099",
            review_request_id="00000000-0000-0000-0000-000000000098",
            action="promote",
            expected_status="new",
            canonical_name="青云官网项目",
            domain="project",
        )
        self.assertEqual(result["status"], "promoted")
        self.assertTrue(result["branch_refresh_job_id"])
        indexer = _Indexer()
        refreshed = BranchRefreshService(repository=repository, indexer=indexer).run_pending()
        self.assertEqual(refreshed, 1)
        self.assertTrue(indexer.updated)
        self.assertTrue(indexer.updated[0].branch_keys)
        self.assertTrue(repository.get_tree(
            scope_type="organization",
            scope_id=context.scope_id,
        )["nodes"])


if __name__ == "__main__":
    unittest.main()
