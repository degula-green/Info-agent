"""Relation expansion: widen one hop when the located entity's node is thin."""

from __future__ import annotations

import unittest

from app.application.rag_service import RAGRetrievalService
from app.config import settings
from app.domain.rag import (
    AccessCheck,
    AuthorizationScope,
    SearchRequest,
    SearchResult,
    normalized_text,
)
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")


class _Embedding:
    model = "test"
    dimensions = 4

    def embed(self, texts):
        return [[0.1] * 4 for _ in texts]


class _Authorization:
    def search_scope(self, *, scope_type, scope_id, **kwargs):
        return AuthorizationScope(scope_type, scope_id, available=True)

    def check_batch(self, *, checks: list[AccessCheck], **kwargs):
        return [True] * len(checks)


def _result(chunk_id: str) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        content=f"content {chunk_id}",
        source={
            "logical_position_key": chunk_id,
            "resource_id": f"r-{chunk_id}",
            "knowledge_item_id": f"item-{chunk_id}",
            "resource_type": "message",
            "content_variant": "display",
            "rag_eligible": True,
        },
    )


class _ScopedIndexer:
    """Answers per entity filter, so a thin node can be simulated exactly."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    def search_bm25(self, request, **kwargs):
        key = kwargs.get("entity_ids")
        self.calls.append(("bm25", key))
        return list(self.answers.get(key or (), []))

    def search_knn(self, request, vector, **kwargs):
        return []

    def search_neighbors(self, request, anchors, **kwargs):
        return []


def _repository_with_relation() -> tuple[InMemoryRagMVPRepository, str, str]:
    repo = InMemoryRagMVPRepository()
    project = repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )["id"]
    related = repo.upsert_entity(
        domain="person", canonical_name="张三", normalized_key=normalized_text("张三"), **SCOPE
    )["id"]
    repo.upsert_entity_relation(
        scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
        source_entity_id=related, target_entity_id=project,
        relation_type="participates_in", confidence=0.9,
        evidence_chunk_ids=["0" * 64],
    )
    return repo, project, related


class RelationExpansionTests(unittest.TestCase):
    def _search(self, repo, indexer, *, tree_mode="tree"):
        original = settings.tree_mode
        object.__setattr__(settings, "tree_mode", tree_mode)
        try:
            service = RAGRetrievalService(
                repository=repo,
                indexer=indexer,
                embedding=_Embedding(),
                authorization=_Authorization(),
            )
            return service.search(SearchRequest(
                query="A项目的服务器", user_id="user-1", **SCOPE,
            ))
        finally:
            object.__setattr__(settings, "tree_mode", original)

    def test_thin_node_expands_one_hop_and_marks_the_origin(self):
        repo, project, related = _repository_with_relation()
        indexer = _ScopedIndexer({
            (project,): [],            # the located entity's own node is empty
            (related,): [_result("c1")],
        })

        response = self._search(repo, indexer)

        self.assertEqual(response.diagnostics["related_entity_count"], 1)
        self.assertEqual(response.diagnostics["related_entities"][0]["entity_id"], related)
        # The evidence chain has to survive the trip: it is what separates
        # "the model said so once" from "several windows agreed".
        self.assertEqual(
            response.diagnostics["related_entities"][0]["evidence_chunk_ids"],
            ["0" * 64],
        )
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].source["retrieval_origin"], "related")

    def test_rich_node_does_not_expand(self):
        repo, project, related = _repository_with_relation()
        # Default top_k is 8, so four hits already reach the 50% threshold that
        # decides the node is not thin.
        indexer = _ScopedIndexer({
            (project,): [_result(f"own{i}") for i in range(4)],
            (related,): [_result("c1")],
        })

        response = self._search(repo, indexer)

        self.assertEqual(response.diagnostics["related_entity_count"], 0)
        self.assertTrue(all(
            item.source.get("retrieval_origin") != "related" for item in response.results
        ))
        self.assertIn("own0", {item.chunk_id for item in response.results})

    def test_expansion_is_tree_mode_only(self):
        repo, project, related = _repository_with_relation()
        indexer = _ScopedIndexer({
            (project,): [],
            (related,): [_result("c1")],
        })

        response = self._search(repo, indexer, tree_mode="shadow")

        self.assertEqual(response.diagnostics["related_entity_count"], 0)


class RelatedEntityLookupTests(unittest.TestCase):
    def test_lookup_respects_direction_confidence_and_limit(self):
        repo, project, related = _repository_with_relation()

        both = repo.find_related_entities(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            entity_ids=[project], direction="both", min_confidence=0.7,
        )
        self.assertEqual([item["entity_id"] for item in both], [related])
        self.assertEqual(both[0]["evidence_chunk_ids"], ["0" * 64])

        # The edge is 张三 -> A项目, so it is inbound from A项目 and outbound
        # from 张三.
        inbound = repo.find_related_entities(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            entity_ids=[project], direction="inbound",
        )
        self.assertEqual(len(inbound), 1)
        outbound = repo.find_related_entities(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            entity_ids=[project], direction="outbound",
        )
        self.assertEqual(outbound, [])

        # A confidence floor above the stored edge drops it.
        self.assertEqual(repo.find_related_entities(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            entity_ids=[project], min_confidence=0.95,
        ), [])

        # An unrelated relation type is filtered out.
        self.assertEqual(repo.find_related_entities(
            scope_type=SCOPE["scope_type"], scope_id=SCOPE["scope_id"],
            entity_ids=[project], relation_types=["works_for"],
        ), [])
