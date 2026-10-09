"""Rollback drill for plan 3.6, re-cut for the entry split.

The earlier version of this file asserted the *fusion* design: in `tree` mode
the scoped and the unscoped channels both entered the RRF pass, so "rollback"
meant "the tree's contribution goes away". That design changed on 2026-10-09 -
traditional and tree retrieval are now separate surfaces - so what has to be
proven is different:

  * the global search box is unaffected by `tree_mode` at all (that is what
    decoupling means in practice, and it is the property the rollback protects)
  * the tree entry returns only the entity-scoped channels
  * when the tree cannot answer it reports that instead of quietly substituting
    the unscoped result - deciding to fall back belongs to the caller

Run against a scoped indexer that answers differently depending on whether an
entity filter was passed.
"""

import unittest

from app.application.rag_service import RAGRetrievalService
from app.config import settings
from app.domain.rag import AccessCheck, AuthorizationScope, SearchRequest, SearchResult
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE_ID = "org-1"


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
            authorized_protected_object_keys=(),
        )

    def check_batch(self, *, checks: list[AccessCheck], **kwargs):
        return [True] * len(checks)


class _ScopedIndexer:
    """Answers with the scoped chunk only when an entity filter is present."""

    def __init__(self):
        self.scoped_calls = 0
        self.global_calls = 0

    def _result(self, chunk_id):
        return SearchResult(
            chunk_id=chunk_id,
            content=chunk_id,
            source={
                "logical_position_key": chunk_id,
                "resource_id": chunk_id,
                "knowledge_item_id": "item-1",
                "resource_type": "message",
                "content_variant": "display",
                "rag_eligible": True,
            },
        )

    def search_bm25(self, request, **kwargs):
        if tuple(kwargs.get("entity_ids") or ()):
            self.scoped_calls += 1
            return [self._result("scoped-chunk")]
        self.global_calls += 1
        return [self._result("global-chunk")]

    def search_knn(self, request, vector, **kwargs):
        return []

    def search_neighbors(self, request, anchors, **kwargs):
        return []


def _repository_with_one_entity():
    repository = InMemoryRagMVPRepository()
    repository.upsert_entity(
        entity_id="entity-1",
        scope_type="organization",
        scope_id=SCOPE_ID,
        domain="project",
        canonical_name="青云项目",
        normalized_key="青云项目",
        registry_version=1,
    )
    return repository


def _search(service, *, query="青云项目进展", entry="tree"):
    return service.search(SearchRequest(
        query=query,
        user_id="user-1",
        scope_type="organization",
        scope_id=SCOPE_ID,
        knowledge_base_ids=("kb-1",),
        entry=entry,
    ))


class RollbackDrill(unittest.TestCase):
    def _run(self, *, mode, entry="tree", sample_rate=1.0):
        repository = _repository_with_one_entity()
        indexer = _ScopedIndexer()
        original_mode = settings.tree_mode
        original_sample = settings.tree_shadow_sample_rate
        object.__setattr__(settings, "tree_mode", mode)
        object.__setattr__(settings, "tree_shadow_sample_rate", sample_rate)
        try:
            service = RAGRetrievalService(
                repository=repository,
                indexer=indexer,
                embedding=_Embedding(),
                authorization=_Authorization(),
            )
            response = _search(service, entry=entry)
        finally:
            object.__setattr__(settings, "tree_mode", original_mode)
            object.__setattr__(settings, "tree_shadow_sample_rate", original_sample)
        return repository, indexer, response

    def test_the_global_search_box_is_untouched_by_every_tree_mode(self):
        # The property the rollback exists to protect: the search box does not
        # depend on the tree, in either direction. It never locates, never asks
        # for scoped channels, and costs no locating latency.
        for mode in ("off", "shadow", "tree"):
            with self.subTest(mode=mode):
                repository, indexer, response = self._run(mode=mode, entry="global")

                self.assertEqual(
                    [item.chunk_id for item in response.results], ["global-chunk"]
                )
                self.assertEqual(
                    response.diagnostics["effective_execution_path"], "traditional"
                )
                self.assertEqual(response.diagnostics["channel_policy"], "hybrid")
                self.assertEqual(response.diagnostics["locate_ms"], 0)
                self.assertEqual(response.diagnostics["resolved_entity_count"], 0)
                self.assertEqual(indexer.scoped_calls, 0)

    def test_the_tree_entry_returns_only_the_scoped_channels(self):
        _, indexer, response = self._run(mode="tree")

        # No global chunk in the result: the tree does not borrow the unscoped
        # result to raise recall.
        self.assertEqual(
            [item.chunk_id for item in response.results], ["scoped-chunk"]
        )
        self.assertEqual(response.diagnostics["channel_policy"], "tree")
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree")
        self.assertGreater(indexer.scoped_calls, 0)
        self.assertGreater(response.diagnostics["locate_ms"], 0)

    def test_shadow_runs_the_tree_but_returns_the_traditional_result(self):
        _, _, response = self._run(mode="shadow")

        # Validation phase: the tree is measured, nothing depends on it.
        self.assertEqual(
            [item.chunk_id for item in response.results], ["global-chunk"]
        )
        self.assertEqual(
            response.diagnostics["effective_execution_path"], "tree_shadow"
        )
        self.assertGreater(response.diagnostics["locate_ms"], 0)

    def test_off_reports_the_tree_as_disabled_instead_of_substituting(self):
        repository, indexer, response = self._run(mode="off")

        # Nothing is returned and nothing is substituted: the caller decides
        # whether to ask for traditional retrieval next.
        self.assertEqual(list(response.results), [])
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree")
        self.assertEqual(response.diagnostics["fallback_reason"], "tree_disabled")
        self.assertEqual(response.diagnostics["locate_ms"], 0)
        self.assertEqual(indexer.scoped_calls, 0)
        self.assertEqual(
            [row["execution_path"] for row in repository.searches], ["tree"]
        )

    def test_sampling_zero_stops_paying_for_shadow_but_still_records_it(self):
        repository, _, response = self._run(mode="shadow", sample_rate=0.0)

        self.assertEqual(
            [item.chunk_id for item in response.results], ["global-chunk"]
        )
        self.assertIs(response.diagnostics["tree_shadow_sampled"], False)
        self.assertEqual(response.diagnostics["fallback_reason"], "shadow_not_sampled")
        self.assertEqual(response.diagnostics["locate_ms"], 0)
        self.assertEqual(len(repository.searches), 1)

    def test_turning_the_tree_off_deletes_nothing(self):
        repository, _, _ = self._run(mode="off")

        entities = repository.entities
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["status"], "active")
