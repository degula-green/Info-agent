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
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


class _Embedding:
    model = "test"
    dimensions = 8

    def embed(self, texts):
        return [[0.1] * 8 for _ in texts]


class _Authorization:
    def search_scope(self, *, scope_type, scope_id, **kwargs):
        return AuthorizationScope(
            scope_type,
            scope_id,
            available=True,
            authorized_organization_ids=(scope_id,),
            authorized_protected_object_keys=("knowledge_original:item-1",),
        )

    def check_batch(self, *, checks: list[AccessCheck], **kwargs):
        return [True] * len(checks)


class _Indexer:
    def __init__(self):
        self.calls = []

    def search_bm25(self, request, **kwargs):
        self.calls.append(("bm25", kwargs))
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
        return []


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
        self.assertTrue(any(call[1].get("branch_keys") for call in indexer.calls))
        self.assertTrue(any(not call[1].get("branch_keys") for call in indexer.calls))
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree_boost")

    def test_logical_dedupe_prefers_protected(self) -> None:
        display = SearchResult("d", "display", score=1.0, source={"logical_position_key": "x", "content_variant": "display"})
        protected = SearchResult("p", "protected", score=0.1, source={"logical_position_key": "x", "content_variant": "protected"})
        values = dedupe_logical_positions([display, protected])
        self.assertEqual([item.chunk_id for item in values], ["p"])


if __name__ == "__main__":
    unittest.main()
