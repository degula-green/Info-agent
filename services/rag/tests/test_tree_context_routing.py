from __future__ import annotations

import unittest

from app.application.retrieval.query_planner import plan_query
from app.domain.memory import FactCandidate, MemoryGraph, TreeSearchRequest
from app.domain.models import AttachmentContext, ChunkRecord, SearchRequest, SearchResult
from app.infrastructure.persistence.repository import InMemoryRagRepository
from app.services.memory_search_service import TreeSearchService


class _Embedding:
    def embed(self, values):
        return [[0.1, 0.2] for _ in values]


class _Authorization:
    def search_scope(self, **_):
        from app.domain.models import AuthorizationScope
        return AuthorizationScope(available=True, objects={})

    def check_batch(self, *, checks, **_):
        return [True] * len(checks)


class _ChunkStore:
    def search_context_chunks(self, **_):
        return [SearchResult(
            chunk_id="neighbor-1", content="补充说明：青云项目由交付组跟进。", score=0.5,
            source={
                "knowledge_item_id": "ki-1", "knowledge_base_id": "kb-1", "organization_id": "org-1",
                "conversation_group_id": "conv-1", "message_id": "msg-2", "sent_at": "2026-09-16T08:05:00Z",
                "auth_resource_type": "knowledge_item", "auth_resource_part": "display", "auth_resource_id": "ki-1",
                "rag_eligible": True,
            },
        )]


class _TreeIndexer:
    def __init__(self, row):
        self.row = row

    def search_tree(self, _request, _vector):
        return [self.row]


def _context() -> AttachmentContext:
    return AttachmentContext(
        attachment_id="att-1", knowledge_item_id="ki-1", knowledge_base_id="kb-1", organization_id="org-1",
        conversation_group_id="conv-1", message_id="msg-1", sent_at="2026-09-16T08:00:00Z",
        file_name="message.txt", mime_type="text/plain",
    )


class TreeContextRoutingTests(unittest.TestCase):
    def test_default_entity_and_conversation_session_routing(self):
        entity = plan_query(SearchRequest("青云项目现在进展怎么样", "u", knowledge_base_id="kb", entry="ai"))
        session = plan_query(SearchRequest("上次群里谁讨论过青云项目", "u", knowledge_base_id="kb", entry="ai"))
        self.assertEqual(entity.preferred_tree_type, "entity")
        self.assertEqual(session.preferred_tree_type, "session")

    def test_time_range_is_resolved_to_utc(self):
        plan = plan_query(SearchRequest("2026年3月青云项目发生了什么", "u", knowledge_base_id="kb", entry="ai"))
        self.assertTrue(plan.time_resolved)
        self.assertEqual(plan.time_after, "2026-02-28T16:00:00Z")
        self.assertEqual(plan.time_before, "2026-03-31T16:00:00Z")

    def test_tree_result_contains_direct_and_neighbor_chunks(self):
        repository = InMemoryRagRepository()
        graph = repository.upsert_memory(
            _context(),
            [ChunkRecord(
                chunk_id="chunk-1", chunk_index=0, chunk_count=1, chunking_version="v1", knowledge_item_id="ki-1",
                attachment_id="att-1", knowledge_base_id="kb-1", content_version=1, attachment_content_version=None,
                auth_acl_version=0, mapping_version="v1", auth_resource_type="attachment", auth_resource_part="content",
                auth_resource_id="att-1", auth_object_key="attachment_content:att-1", knowledge_scope=None, access_scope=None,
                organization_id="org-1", owner_user_id=None, conversation_group_id="conv-1", part_kind="message_display",
                content_access_required=False, title=None, content="青云项目已进入交付阶段。", content_hash="0" * 64,
                content_visibility="display", rag_eligible=True, sent_at="2026-09-16T08:00:00Z", message_id="msg-1",
            )],
            [FactCandidate(fact_type="state", text="青云项目已进入交付阶段", subject="青云项目", topic="进度", phase="交付", chunk_ids=("chunk-1",))],
        )
        fact = next(row for row in graph.fact_projections if row["tree_type"] == "entity")
        row = {
            "tree": {"tree_id": fact["tree_id"], "tree_type": "entity", "subject_key": "青云项目"},
            "path": [{"node_type": "root"}, {"node_type": "internal"}, {"node_type": "leaf"}],
            "leaf_node_id": fact["node_id"], "visibility": "display", "fact": fact, "score": 1.0,
        }
        service = TreeSearchService(
            indexer=_TreeIndexer(row), repository=repository, embedding_provider=_Embedding(),
            authorization=_Authorization(), chunk_store=_ChunkStore(),
        )
        result = service.search(TreeSearchRequest(
            query="青云项目现在进展怎么样", user_id="u", organization_id="org-1", knowledge_base_id="kb-1",
            knowledge_base_ids=("kb-1",), tree_types=("entity",),
        ))
        chunks = result["items"][0]["context_chunks"]
        self.assertEqual(chunks[0]["relation"], "direct_evidence")
        self.assertEqual(chunks[0]["auth_resource_type"], "attachment")
        self.assertEqual(chunks[0]["auth_resource_part"], "content")
        self.assertEqual(chunks[0]["auth_resource_id"], "att-1")
        self.assertEqual(chunks[1]["relation"], "neighbor")
        self.assertEqual(result["routing"]["execution_path"], "entity_tree")


if __name__ == "__main__":
    unittest.main()
