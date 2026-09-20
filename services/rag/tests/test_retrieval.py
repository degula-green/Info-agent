from __future__ import annotations

import unittest

from app.application.retrieval.fulltext_retriever import FullTextRetriever
from app.application.retrieval.hybrid_retriever import HybridRetriever
from app.application.retrieval.context_assembler import assemble_context
from app.domain.models import AuthorizationScope, SearchRequest, SearchResult


class _Embedding:
    model = "test"
    dimensions = 2

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, values):
        self.calls += 1
        return [[1.0, 0.0] for _ in values]


class _Authorization:
    def __init__(self, scope: AuthorizationScope) -> None:
        self.scope = scope
        self.check_calls = 0

    def search_scope(self, **_):
        return self.scope

    def check_batch(self, *, checks, **_):
        self.check_calls += 1
        return [True] * len(checks)


class _Store:
    def __init__(self) -> None:
        self.last = None

    def parallel_search(self, **kwargs):
        self.last = kwargs
        return {
            "display_bm25": [SearchResult("display", "visible", 1.0, source={"knowledge_item_id": "ki-1", "auth_resource_type": "knowledge_item", "auth_resource_part": "display", "auth_resource_id": "ki-1", "rag_eligible": True})],
            "protected_bm25": [SearchResult("protected", "secret", 0.9, source={"knowledge_item_id": "ki-2", "auth_resource_type": "knowledge_item", "auth_resource_part": "original", "auth_resource_id": "ki-2", "rag_eligible": True})],
        }


class RetrievalTests(unittest.TestCase):
    def test_citation_preserves_document_name_for_display_aggregation(self) -> None:
        bundle = assemble_context([SearchResult(
            "chunk-1", "document content", 1.0,
            source={"knowledge_item_id": "ki-1", "attachment_id": "att-1", "file_name": "report.docx"},
        )])
        self.assertEqual(bundle.citations[0]["file_name"], "report.docx")

    def test_context_numbers_displayable_sources_not_document_chunks(self) -> None:
        message = SearchResult("message-chunk", "预计10月份进行招新", 3.0, source={
            "knowledge_item_id": "message-1", "message_id": "message-1", "source_resource_id": "message-1",
        })
        document_fact = SearchResult("fact-1", "青云飞鹏小组有系统培养路线", 2.0, source={
            "knowledge_item_id": "doc-1", "attachment_id": "att-1",
        })
        document_chunk = SearchResult("chunk-1", "常见问题正文第一段", 1.0, source={
            "knowledge_item_id": "doc-1", "attachment_id": "att-1", "file_name": "常见问题.docx",
        })
        bundle = assemble_context([message, document_fact, document_chunk])
        self.assertEqual([citation["source_kind"] for citation in bundle.citations], ["message", "document"])
        self.assertEqual([citation["rank"] for citation in bundle.citations], [1, 2])
        self.assertEqual(bundle.citations[1]["file_name"], "常见问题.docx")
        self.assertEqual(bundle.citations[1]["evidence_chunk_ids"], ["fact-1", "chunk-1"])
        self.assertIn("[资料 1] 飞书消息", bundle.text)
        self.assertEqual(bundle.text.count("[资料 2] 常见问题.docx"), 1)

    def test_protected_search_contains_authorization_terms_filter(self) -> None:
        store = _Store()
        auth = _Authorization(AuthorizationScope(objects={"knowledge_original": ("knowledge_original:ki-2",)}))
        embedding = _Embedding()
        results, diagnostics = HybridRetriever(store=store, authorization=auth, embedding=embedding).retrieve(SearchRequest("why", "user", organization_id="org"))
        self.assertEqual({item.chunk_id for item in results}, {"display", "protected"})
        self.assertTrue(any("auth_object_key" in item.get("terms", {}) for item in store.last["protected_filters"]))
        self.assertEqual(embedding.calls, 1)
        self.assertEqual(diagnostics.authorized_count, 2)

    def test_unavailable_scope_does_not_query_protected_index(self) -> None:
        store = _Store()
        auth = _Authorization(AuthorizationScope(available=False))
        results, _ = HybridRetriever(store=store, authorization=auth, embedding=_Embedding()).retrieve(SearchRequest("term", "user"))
        self.assertNotIn("protected_bm25", store.last)
        self.assertEqual([item.chunk_id for item in results], ["display"])

    def test_knowledge_search_does_not_call_embedding(self) -> None:
        store = _Store()
        embedding = _Embedding()
        auth = _Authorization(AuthorizationScope(available=False))
        FullTextRetriever(HybridRetriever(store=store, authorization=auth, embedding=embedding)).retrieve(SearchRequest("term", "user", entry="knowledge"))
        self.assertEqual(embedding.calls, 0)
        self.assertIsNone(store.last["query_vector"])

    def test_source_scope_is_a_mandatory_chunk_filter(self) -> None:
        store = _Store()
        auth = _Authorization(AuthorizationScope(available=False))
        request = SearchRequest(
            "制度细节", "user", entry="ai",
            source_attachment_ids=("att-1",), source_knowledge_item_ids=("ki-1",),
        )
        HybridRetriever(store=store, authorization=auth, embedding=_Embedding()).retrieve(request)
        filters = store.last["display_filters"]
        source_filter = next(item["bool"] for item in filters if "bool" in item and "should" in item["bool"] and any("attachment_id" in value.get("terms", {}) for value in item["bool"]["should"]))
        self.assertEqual(source_filter["minimum_should_match"], 1)
        self.assertEqual(source_filter["should"], [
            {"terms": {"attachment_id": ["att-1"]}},
            {"terms": {"knowledge_item_id": ["ki-1"]}},
        ])


if __name__ == "__main__":
    unittest.main()
