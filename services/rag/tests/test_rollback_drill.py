"""Rollback drill for plan 3.6, encoded so it can be re-run instead of retold.

The plan's rollback is two steps - `tree_mode` to shadow, then to off - with the
promise that retrieval results stop depending on the tree and nothing is
deleted. Each step is asserted here against a scoped indexer that answers
differently depending on whether entity filters were passed.
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


def _search(service, *, query="青云项目进展"):
    return service.search(SearchRequest(
        query=query,
        user_id="user-1",
        scope_type="organization",
        scope_id=SCOPE_ID,
        knowledge_base_ids=("kb-1",),
    ))


class RollbackDrill(unittest.TestCase):
    def _run_mode(self, mode, *, sample_rate=1.0):
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
            response = _search(service)
        finally:
            object.__setattr__(settings, "tree_mode", original_mode)
            object.__setattr__(settings, "tree_shadow_sample_rate", original_sample)
        return repository, indexer, response

    def test_step_0_before_rollback_the_tree_narrows_the_result(self):
        _, indexer, response = self._run_mode("tree")

        # Tree-first, not tree-only: the scoped branch is added and the global
        # branch stays as a backstop so a wrong entity match cannot hide the
        # answer. Both are present; only the tree path produces the scoped one.
        self.assertEqual(
            {item.chunk_id for item in response.results},
            {"scoped-chunk", "global-chunk"},
        )
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree")
        self.assertGreater(indexer.scoped_calls, 0)
        # Location really ran, so the diagnostics are the thing being rolled
        # back to a safe state, not a broken pipeline.
        self.assertGreater(response.diagnostics["locate_ms"], 0)

    def test_step_1_shadow_returns_the_unscoped_result_but_keeps_diagnostics(self):
        repository, _, response = self._run_mode("shadow")

        # Results no longer depend on the tree...
        self.assertEqual([item.chunk_id for item in response.results], ["global-chunk"])
        self.assertEqual(
            response.diagnostics["effective_execution_path"], "tree_shadow"
        )
        # ...but the location work still runs, so the failure can be diagnosed.
        self.assertGreater(response.diagnostics["locate_ms"], 0)
        self.assertEqual(
            [row["execution_path"] for row in repository.searches], ["tree_shadow"]
        )

    def test_step_2_off_stops_locating_entirely(self):
        repository, indexer, response = self._run_mode("off")

        self.assertEqual([item.chunk_id for item in response.results], ["global-chunk"])
        self.assertEqual(response.diagnostics["effective_execution_path"], "traditional")
        # No location means no registry read, no embedding, no L4.
        self.assertEqual(response.diagnostics["locate_ms"], 0)
        self.assertEqual(response.diagnostics["resolved_entity_count"], 0)
        self.assertEqual(indexer.scoped_calls, 0)
        self.assertEqual([row["execution_path"] for row in repository.searches], [
            "traditional"
        ])

    def test_shadow_and_off_agree_on_results(self):
        # The rollback promise: turning the tree off must not change what the
        # caller sees, only which path produced it.
        _, _, shadow = self._run_mode("shadow")
        _, _, off = self._run_mode("off")

        self.assertEqual(
            [item.chunk_id for item in shadow.results],
            [item.chunk_id for item in off.results],
        )

    def test_rollback_deletes_nothing(self):
        repository, _, _ = self._run_mode("off")

        # Rollback disables, it does not clean up: entity, mounts and vectors
        # stay put so the tree can be switched back on.
        entities = repository.entities
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["status"], "active")

    def test_sampled_out_shadow_is_recorded_as_not_sampled(self):
        # Sampling at 0 is the cheapest form of "stop paying for shadow": the
        # request still lands in history, flagged, instead of vanishing.
        repository, _, response = self._run_mode("shadow", sample_rate=0.0)

        self.assertEqual([item.chunk_id for item in response.results], ["global-chunk"])
        self.assertIs(response.diagnostics["tree_shadow_sampled"], False)
        self.assertEqual(response.diagnostics["locate_ms"], 0)
        self.assertEqual(len(repository.searches), 1)
