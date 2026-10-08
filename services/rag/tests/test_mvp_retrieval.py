from __future__ import annotations

import unittest
from dataclasses import replace

from app.application.rag_service import RAGRetrievalService, dedupe_logical_positions
from app.config import settings
from app.domain.rag import (
    AccessCheck,
    AuthorizationScope,
    Entity,
    SearchRequest,
    SearchResult,
)
from app.infrastructure.rag_elasticsearch import RagChunkIndex, _bm25_query, _filters
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


class _Embedding:
    model = "test"
    dimensions = 8

    def embed(self, texts):
        return [[0.1] * 8 for _ in texts]


class _Authorization:
    def __init__(self):
        self.batch_sizes = []

    def search_scope(self, *, scope_type, scope_id, **kwargs):
        return AuthorizationScope(
            scope_type,
            scope_id,
            available=True,
            authorized_organization_ids=(scope_id,),
            authorized_protected_object_keys=("knowledge_original:item-1",),
        )

    def check_batch(self, *, checks: list[AccessCheck], **kwargs):
        self.batch_sizes.append(len(checks))
        return [True] * len(checks)


class _Indexer:
    def __init__(self, *, bm25_results=None, knn_results=None, neighbors=None):
        self.calls = []
        self.bm25_results = bm25_results
        self.knn_results = knn_results or []
        self.neighbors = neighbors or []

    def search_bm25(self, request, **kwargs):
        self.calls.append(("bm25", kwargs))
        if self.bm25_results is not None:
            return list(self.bm25_results)
        return [SearchResult(
            chunk_id="c1",
            content="项目状态",
            source={
                "logical_position_key": "l1",
                "resource_id": "r1",
                "knowledge_item_id": "item-1",
                "resource_type": "message",
                "content_variant": "display",
                "rag_eligible": True,
            },
        )]

    def search_knn(self, request, vector, **kwargs):
        self.calls.append(("knn", kwargs))
        return list(self.knn_results)

    def search_neighbors(self, request, anchors, **kwargs):
        self.calls.append(("neighbors", kwargs))
        return list(self.neighbors)


class _ProtectedIndexer(_Indexer):
    def __init__(self, *, protected_variants=None, **kwargs):
        super().__init__(**kwargs)
        self.protected_variants = list(protected_variants or [])

    def search_protected_variants(self, request, knowledge_item_ids):
        self.calls.append(("protected", {"ids": tuple(knowledge_item_ids)}))
        return [
            item
            for item in self.protected_variants
            if item.source.get("knowledge_item_id") in knowledge_item_ids
        ]


class _RecordingElasticsearch:
    def __init__(self) -> None:
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"hits": {"hits": []}}


class RetrievalTests(unittest.TestCase):
    def test_boost_keeps_global_and_branch_channels(self) -> None:
        repository = InMemoryRagMVPRepository()
        repository.upsert_entity(
            entity_id="entity-1",
            scope_type="organization",
            scope_id="org-1",
            domain="project",
            canonical_name="青云项目",
            normalized_key="青云项目",
            registry_version=1,
        )
        indexer = _Indexer()
        original = settings.tree_mode
        object.__setattr__(settings, "tree_mode", "boost")
        try:
            service = RAGRetrievalService(
                repository=repository,
                indexer=indexer,
                embedding=_Embedding(),
                authorization=_Authorization(),
            )
            response = service.search(SearchRequest(
                query="青云项目进展",
                user_id="user-1",
                scope_type="organization",
                scope_id="org-1",
                knowledge_base_ids=("kb-1",),
            ))
        finally:
            object.__setattr__(settings, "tree_mode", original)
        self.assertEqual(len(response.results), 1)
        self.assertTrue(any(call[1].get("entity_ids") for call in indexer.calls))
        self.assertTrue(any(not call[1].get("entity_ids") for call in indexer.calls))
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree_boost")
        self.assertEqual(response.diagnostics["authorization_candidate_count"], 1)
        self.assertEqual(service.authorization.batch_sizes, [1])

    def test_logical_dedupe_prefers_protected(self) -> None:
        display = SearchResult("d", "display", score=1.0, source={"logical_position_key": "x", "content_variant": "display"})
        protected = SearchResult("p", "protected", score=0.1, source={"logical_position_key": "x", "content_variant": "protected"})
        values = dedupe_logical_positions([display, protected])
        self.assertEqual([item.chunk_id for item in values], ["p"])

    def test_authorized_candidate_upgrades_to_protected_variant(self) -> None:
        display = SearchResult(
            chunk_id="d1",
            content="[密码已脱敏]",
            score=1.0,
            source={
                "logical_position_key": "l1",
                "resource_id": "r1",
                "resource_type": "message",
                "knowledge_item_id": "item-1",
                "content_variant": "display",
                "rag_eligible": True,
            },
        )
        protected = SearchResult(
            chunk_id="p1",
            content="密码123456",
            score=0.5,
            source={
                "logical_position_key": "l1",
                "resource_id": "r1",
                "resource_type": "message",
                "knowledge_item_id": "item-1",
                "content_variant": "protected",
                "rag_eligible": True,
            },
        )
        indexer = _ProtectedIndexer(
            bm25_results=[display],
            protected_variants=[protected],
        )
        service = RAGRetrievalService(
            repository=InMemoryRagMVPRepository(),
            indexer=indexer,
            embedding=_Embedding(),
            authorization=_Authorization(),
        )
        response = service.search(SearchRequest(
            query="密码",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            include_protected=True,
        ))
        self.assertEqual([item.content for item in response.results], ["密码123456"])
        self.assertEqual(service.authorization.batch_sizes, [1, 1])
        self.assertEqual(indexer.calls[-1][0], "protected")

    def test_protected_channel_is_searched_without_enumerated_keys(self) -> None:
        client = _RecordingElasticsearch()
        index = RagChunkIndex(client)
        request = SearchRequest(
            query="密码",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            include_protected=True,
        )

        index.search_bm25(request, protected_object_keys=())

        indexes = [call["index"] for call in client.calls]
        self.assertIn(settings.elasticsearch_display_read_index, indexes)
        self.assertIn(settings.elasticsearch_protected_read_index, indexes)

    def test_metadata_filters_and_empty_query(self) -> None:
        request = SearchRequest(
            query="",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            sender_ids=("sender-1",),
            sender_names=("张三",),
            conversation_ids=("conversation-1",),
            conversation_names=("财务群",),
            resource_ids=("resource-1",),
            resource_types=("attachment",),
            file_extensions=("xlsx",),
            message_types=("file",),
        )
        filters = _filters(request)
        as_text = str(filters)
        self.assertIn("sender_identity_id", as_text)
        self.assertIn("sender_display_name", as_text)
        self.assertIn("source_conversation_id", as_text)
        self.assertIn("source_conversation_name", as_text)
        self.assertIn("resource_id", as_text)
        self.assertIn("file_extension", as_text)
        query = _bm25_query("", filters)
        self.assertNotIn("must", query)
        self.assertEqual(query["bool"]["filter"], filters)

    def test_qa_conversation_id_does_not_filter_source_conversation(self) -> None:
        qa_request = SearchRequest(
            query="follow-up",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            conversation_id="qa-conversation-1",
        )
        self.assertNotIn(
            {"term": {"source_conversation_id": "qa-conversation-1"}},
            _filters(qa_request),
        )
        source_request = replace(qa_request, source_conversation_id="source-chat-1")
        self.assertIn(
            {"term": {"source_conversation_id": "source-chat-1"}},
            _filters(source_request),
        )

    def test_qa_gate_drops_weak_vector_only_candidate(self) -> None:
        weak = SearchResult(
            chunk_id="weak",
            content="weak match",
            score=0.64,
            source={
                "logical_position_key": "weak",
                "resource_id": "message-1",
                "knowledge_item_id": "item-weak",
                "resource_type": "message",
                "content_variant": "display",
                "rag_eligible": True,
            },
        )
        service = RAGRetrievalService(
            repository=InMemoryRagMVPRepository(),
            indexer=_Indexer(bm25_results=[], knn_results=[weak]),
            embedding=_Embedding(),
            authorization=_Authorization(),
        )
        response = service.search(SearchRequest(
            query="weak query",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="ai",
        ))
        self.assertEqual(response.results, [])
        self.assertEqual(response.diagnostics["score_gate_dropped_count"], 1)

    def test_qa_expands_neighbor_chunk_from_anchor(self) -> None:
        anchor = SearchResult(
            chunk_id="anchor-question",
            content="作息时间如何安排？",
            score=0.89,
            source={
                "logical_position_key": "anchor",
                "resource_id": "attachment-1",
                "knowledge_item_id": "item-attachment",
                "resource_type": "attachment",
                "content_variant": "display",
                "content_version": 1,
                "chunk_index": 15,
                "chunk_count": 19,
                "file_name": "faq.docx",
                "rag_eligible": True,
            },
        )
        neighbor = SearchResult(
            chunk_id="neighbor-answer",
            content="作息安排如下……",
            score=0,
            source={
                "logical_position_key": "neighbor",
                "resource_id": "attachment-1",
                "knowledge_item_id": "item-attachment",
                "resource_type": "attachment",
                "content_variant": "display",
                "content_version": 1,
                "chunk_index": 16,
                "chunk_count": 19,
                "file_name": "faq.docx",
                "rag_eligible": True,
            },
        )
        service = RAGRetrievalService(
            repository=InMemoryRagMVPRepository(),
            indexer=_Indexer(bm25_results=[], knn_results=[anchor], neighbors=[neighbor]),
            embedding=_Embedding(),
            authorization=_Authorization(),
        )
        response = service.search(SearchRequest(
            query="作息是什么",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="ai",
        ))
        self.assertEqual([item.chunk_id for item in response.results], [
            "anchor-question",
            "neighbor-answer",
        ])
        self.assertEqual(response.results[0].source["retrieval_role"], "anchor")
        self.assertEqual(response.results[1].source["retrieval_role"], "neighbor")
        self.assertEqual(response.diagnostics["neighbor_candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
