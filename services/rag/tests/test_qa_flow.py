from __future__ import annotations

import unittest
from unittest.mock import patch

from app.domain.models import SearchRequest, SearchResult
from app.infrastructure.persistence.repository import InMemoryRagRepository
from app.infrastructure.qa import QAUnavailable
from app.services.search_service import QAConversationNotFound, RagSearchService, _dedupe_results


class _Embedding:
    model = "test"
    dimensions = 2

    def embed(self, values):
        return [[1.0, 0.0] for _ in values]


class _Authorization:
    def search_scope(self, **_):
        from app.domain.models import AuthorizationScope
        return AuthorizationScope(available=False)

    def check_batch(self, *, checks, **_):
        return [True] * len(checks)


class _Store:
    def __init__(self):
        self.calls = []

    def parallel_search(self, **kwargs):
        self.calls.append(kwargs)
        return {"display_bm25": [SearchResult(
            "chunk-1", "星河项目已完成验收。", 1.0,
            source={
                "knowledge_item_id": "ki-1", "knowledge_base_id": "kb-1",
                "auth_resource_type": "knowledge_item", "auth_resource_part": "display",
                "auth_resource_id": "ki-1", "rag_eligible": True,
            },
        )]}


class _Answer:
    def generate(self, question, results):
        return "依据资料，星河项目已完成验收。"


class _BrokenAnswer:
    def generate(self, question, results):
        raise QAUnavailable("provider timeout")


class _NoTree:
    def search(self, request):
        return {"items": [], "diagnostics": {}}


class _SourceTree:
    def search(self, request):
        return {"items": [{
            "fact": None,
            "sources": [{"source_id": "source-1", "attachment_id": "att-7", "knowledge_item_id": "ki-7"}],
            "path": [], "tree": {"tree_type": "entity"}, "score": 1.0,
        }], "diagnostics": {}}


class QAFlowTests(unittest.TestCase):
    def test_final_context_keeps_multiple_chunks_but_caps_each_document(self):
        results = [SearchResult(
            f"chunk-{index}", f"content-{index}", 10.0 - index,
            source={"knowledge_item_id": "ki-1", "attachment_id": "att-1"},
        ) for index in range(3)]
        deduped = _dedupe_results(results, 8)
        self.assertEqual([item.chunk_id for item in deduped], ["chunk-0", "chunk-1"])

    def _service(self, answer_provider):
        return RagSearchService(
            store=_Store(), authorization=_Authorization(), embedding=_Embedding(),
            repository=InMemoryRagRepository(), tree_search_service=_NoTree(),
            answer_provider=answer_provider,
        )

    def test_successful_answer_persists_exchange_and_scope(self):
        service = self._service(_Answer())
        result = service.answer(SearchRequest(
            "项目状态", "user-a", knowledge_base_ids=("kb-1", "kb-2"), qa_mode="quick",
        ))
        self.assertEqual(result["answer"], "依据资料，星河项目已完成验收。")
        detail = service.repository.get_qa_conversation(user_id="user-a", conversation_id=result["conversation_id"])
        self.assertEqual(detail["knowledge_base_ids"], ["kb-1", "kb-2"])
        self.assertEqual([message["role"] for message in detail["messages"]], ["user", "assistant"])
        self.assertEqual(detail["messages"][1]["status"], "completed")

    def test_failed_answer_persists_error_message(self):
        service = self._service(_BrokenAnswer())
        result = service.answer(SearchRequest("项目状态", "user-a", knowledge_base_id="kb-1"))
        self.assertIsNone(result["answer"])
        detail = service.repository.get_qa_conversation(user_id="user-a", conversation_id=result["conversation_id"])
        assistant = detail["messages"][1]
        self.assertEqual(assistant["status"], "failed")
        self.assertEqual(assistant["error_message"], "provider timeout")

    def test_conversation_id_is_owner_scoped(self):
        service = self._service(_Answer())
        first = service.answer(SearchRequest("项目状态", "user-a", knowledge_base_id="kb-1"))
        with self.assertRaises(QAConversationNotFound):
            service.answer(SearchRequest("越权问题", "user-b", knowledge_base_id="kb-1", conversation_id=first["conversation_id"]))

    def test_document_question_runs_scoped_and_global_branches(self):
        store = _Store()
        service = RagSearchService(
            store=store, authorization=_Authorization(), embedding=_Embedding(),
            repository=InMemoryRagRepository(), tree_search_service=_SourceTree(), answer_provider=_Answer(),
        )
        result = service.answer(SearchRequest("制度中有哪些具体要求", "user-a", knowledge_base_id="kb-1"))
        self.assertEqual(result["diagnostics"]["fallback_level"], 0)
        filters = store.calls[0]["display_filters"]
        scope = next(value["bool"] for value in filters if "bool" in value and any("attachment_id" in part.get("terms", {}) for part in value["bool"].get("should", [])))
        self.assertEqual(scope["should"], [
            {"terms": {"attachment_id": ["att-7"]}},
            {"terms": {"knowledge_item_id": ["ki-7"]}},
        ])
        # The tree no longer gates retrieval. The tree-scoped branch and the
        # unscoped Chunk branch both run and are fused, instead of the unscoped
        # branch being reachable only when the tree produced nothing.
        self.assertEqual(len(store.calls), 2)
        self.assertEqual(result["diagnostics"]["fused_branches"], ["global", "tree_sources"])
        self.assertFalse(any(
            any("attachment_id" in part.get("terms", {}) for part in value.get("bool", {}).get("should", []))
            for value in store.calls[1]["display_filters"]
        ))


if __name__ == "__main__":
    unittest.main()
