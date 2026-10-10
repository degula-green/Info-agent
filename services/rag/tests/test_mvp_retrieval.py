from __future__ import annotations

import inspect
import threading
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
from app.infrastructure.persistence.mvp import (
    InMemoryRagMVPRepository,
    PostgresRagMVPRepository,
)


class _StrictRecordSearchRepository(InMemoryRagMVPRepository):
    """The in-memory double enforces the production `record_search` signature.

    InMemory takes ``**value``, so a caller that forgets a keyword fails only
    against the strict Postgres method - and the caller's ``except Exception``
    then swallows the TypeError. That is exactly how the anchor-context read
    went unrecorded in production while the tests stayed green. The required
    set is derived from the real method, not copied by hand.
    """

    REQUIRED = frozenset(
        name
        for name, parameter in inspect.signature(
            PostgresRagMVPRepository.record_search
        ).parameters.items()
        if name != "self" and parameter.default is inspect.Parameter.empty
    )

    def record_search(self, **value):
        missing = self.REQUIRED - set(value)
        assert not missing, f"record_search 缺少必需参数: {sorted(missing)}"
        return super().record_search(**value)


class _Embedding:
    model = "test"
    dimensions = 8

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
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
        self.requests = []
        self.bm25_results = bm25_results
        self.knn_results = knn_results or []
        self.neighbors = neighbors or []

    def search_bm25(self, request, **kwargs):
        self.requests.append(request)
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
        self.requests.append(request)
        self.calls.append(("knn", kwargs))
        return list(self.knn_results)

    def search_neighbors(self, request, anchors, **kwargs):
        self.calls.append(("neighbors", kwargs))
        return list(self.neighbors)

    def search_message_context(self, request, anchors, radius=2, **kwargs):
        self.calls.append(("context", {"anchors": len(anchors), "radius": radius}))
        return [SearchResult(
            chunk_id="ctx-1",
            content="锚点周边消息",
            source={
                "logical_position_key": "l-ctx",
                "resource_id": "r-ctx",
                "knowledge_item_id": "item-1",
                "resource_type": "message",
                "content_variant": "display",
                "rag_eligible": True,
            },
        )]


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


class _BarrierAuthorization(_Authorization):
    def __init__(self, barrier: threading.Barrier):
        super().__init__()
        self.barrier = barrier

    def search_scope(self, **kwargs):
        self.barrier.wait(timeout=2)
        return super().search_scope(**kwargs)


class _BarrierEmbedding(_Embedding):
    def __init__(self, barrier: threading.Barrier):
        super().__init__()
        self.barrier = barrier

    def embed(self, texts):
        self.barrier.wait(timeout=2)
        return super().embed(texts)


class _BarrierIndexer(_Indexer):
    def __init__(self, barrier: threading.Barrier):
        super().__init__()
        self.barrier = barrier

    def search_bm25(self, request, **kwargs):
        values = super().search_bm25(request, **kwargs)
        self.barrier.wait(timeout=2)
        return values

    def search_knn(self, request, vector, **kwargs):
        values = super().search_knn(request, vector, **kwargs)
        self.barrier.wait(timeout=2)
        return values


class _RecordingElasticsearch:
    def __init__(self) -> None:
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"hits": {"hits": []}}


class RetrievalTests(unittest.TestCase):
    def test_channel_selection_never_mixes_the_two_surfaces(self) -> None:
        from app.application.rag_service import select_retrieval_channels

        global_branches = {"bm25": ["g"]}
        scoped = {"branch_bm25": ["s"]}

        # A traditional entry always gets the unscoped result, whatever the
        # tree's rollout state is.
        effective, path, reason = select_retrieval_channels(
            policy="hybrid", tree_mode="tree", global_branches=global_branches,
            scoped_branches=scoped, tree_reason=None, degraded=[],
        )
        self.assertEqual((path, reason), ("traditional", None))
        self.assertEqual(effective, global_branches)

        # A tree entry gets the scoped channels - and only those. The unscoped
        # channels must not be borrowed to raise recall.
        effective, path, reason = select_retrieval_channels(
            policy="tree", tree_mode="tree", global_branches=global_branches,
            scoped_branches=scoped, tree_reason=None, degraded=[],
        )
        self.assertEqual((path, reason), ("tree", None))
        self.assertEqual(set(effective), {"branch_bm25"})
        self.assertNotIn("bm25", effective)

        # shadow still returns the traditional result: validation phase.
        effective, path, reason = select_retrieval_channels(
            policy="tree", tree_mode="shadow", global_branches=global_branches,
            scoped_branches=scoped, tree_reason=None, degraded=[],
        )
        self.assertEqual(path, "tree_shadow")
        self.assertEqual(effective, global_branches)

        effective, path, reason = select_retrieval_channels(
            policy="keyword",
            tree_mode="tree",
            global_branches=global_branches,
            scoped_branches=scoped,
            tree_reason=None,
            degraded=[],
        )
        self.assertEqual((path, reason), ("keyword", None))
        self.assertEqual(effective, global_branches)

        # The tree surface reports why it cannot answer instead of substituting.
        for reason_in in ("tree_disabled", "no_entity_match", "shadow_not_sampled"):
            with self.subTest(reason=reason_in):
                effective, path, reason = select_retrieval_channels(
                    policy="tree", tree_mode="tree", global_branches=global_branches,
                    scoped_branches={}, tree_reason=reason_in, degraded=[],
                )
                self.assertEqual((path, reason), ("tree", reason_in))
                self.assertEqual(effective, {})

        # An empty scope and a failed branch are derived here, same treatment.
        _, path, reason = select_retrieval_channels(
            policy="tree", tree_mode="tree", global_branches=global_branches,
            scoped_branches={}, tree_reason=None, degraded=[],
        )
        self.assertEqual((path, reason), ("tree", "empty_scope"))
        _, path, reason = select_retrieval_channels(
            policy="tree", tree_mode="tree", global_branches=global_branches,
            scoped_branches=scoped, tree_reason=None, degraded=["branch_failed"],
        )
        self.assertEqual((path, reason), ("tree", "branch_failed"))

    def test_the_tree_entry_returns_only_the_scoped_channels(self) -> None:
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
        object.__setattr__(settings, "tree_mode", "tree")
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
                entry="tree",
            ))
        finally:
            object.__setattr__(settings, "tree_mode", original)
        self.assertEqual(len(response.results), 1)
        # Scoped channels were used...
        self.assertTrue(any(call[1].get("entity_ids") for call in indexer.calls))
        # ...and the unscoped ones were never queried: the tree entry does not
        # borrow the global result to raise recall.
        self.assertFalse(any(not call[1].get("entity_ids") for call in indexer.calls))
        self.assertEqual(response.diagnostics["channel_policy"], "tree")
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree")
        self.assertEqual(response.diagnostics["authorization_candidate_count"], 1)
        # The 80ms location gate reads these two fields, so a tree-mode search
        # has to carry them out of the request.
        self.assertGreater(response.diagnostics["locate_ms"], 0)
        self.assertIn("L1", response.diagnostics["locate_layer_ms"])
        self.assertEqual(service.authorization.batch_sizes, [1])

    def test_rollout_whitelist_promotes_a_named_scope_only(self) -> None:
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
        original_mode = settings.tree_mode
        original_rollout = settings.tree_rollout_scopes
        object.__setattr__(settings, "tree_mode", "shadow")
        object.__setattr__(settings, "tree_rollout_scopes", "organization:org-1")
        try:
            service = RAGRetrievalService(
                repository=repository,
                indexer=_Indexer(),
                embedding=_Embedding(),
                authorization=_Authorization(),
            )
            promoted = service.search(SearchRequest(
                query="青云项目进展",
                user_id="user-1",
                scope_type="organization",
                scope_id="org-1",
                knowledge_base_ids=("kb-1",),
                entry="tree",
            ))
            untouched = service.search(SearchRequest(
                query="青云项目进展",
                user_id="user-1",
                scope_type="organization",
                scope_id="org-2",
                knowledge_base_ids=("kb-1",),
                entry="tree",
            ))
        finally:
            object.__setattr__(settings, "tree_mode", original_mode)
            object.__setattr__(settings, "tree_rollout_scopes", original_rollout)

        self.assertEqual(promoted.diagnostics["tree_mode"], "tree")
        self.assertEqual(promoted.diagnostics["tree_mode_default"], "shadow")
        self.assertEqual(promoted.diagnostics["effective_execution_path"], "tree")
        # The neighbouring scope keeps the deployment default.
        self.assertEqual(untouched.diagnostics["tree_mode"], "shadow")
        self.assertEqual(
            untouched.diagnostics["effective_execution_path"], "tree_shadow"
        )

    def test_traditional_entries_never_locate_even_with_an_entity_present(self) -> None:
        # The search box and the agent's content search share the traditional
        # surface. An entity exists in the scope, so a tree-aware request would
        # locate - these entries must not, in any tree_mode.
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
        original = settings.tree_mode
        object.__setattr__(settings, "tree_mode", "tree")
        try:
            for entry in ("global", "knowledge", "content"):
                with self.subTest(entry=entry):
                    indexer = _Indexer()
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
                        entry=entry,
                    ))

                    self.assertEqual(response.diagnostics["channel_policy"], "hybrid")
                    self.assertEqual(
                        response.diagnostics["effective_execution_path"],
                        "traditional",
                    )
                    self.assertEqual(response.diagnostics["locate_ms"], 0)
                    self.assertEqual(response.diagnostics["resolved_entity_count"], 0)
                    self.assertFalse(
                        any(call[1].get("entity_ids") for call in indexer.calls)
                    )
        finally:
            object.__setattr__(settings, "tree_mode", original)

    def test_keyword_entry_uses_bm25_only(self) -> None:
        repository = InMemoryRagMVPRepository()
        indexer = _Indexer()
        embedding = _Embedding()
        service = RAGRetrievalService(
            repository=repository,
            indexer=indexer,
            embedding=embedding,
            authorization=_Authorization(),
        )

        response = service.search(SearchRequest(
            query="服务器 配置",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            knowledge_base_ids=("kb-1",),
            entry="keyword",
        ))

        self.assertEqual(response.diagnostics["channel_policy"], "keyword")
        self.assertEqual(response.diagnostics["effective_execution_path"], "keyword")
        self.assertEqual(response.diagnostics["locate_ms"], 0)
        self.assertEqual(response.diagnostics["resolved_entity_count"], 0)
        self.assertEqual(embedding.calls, [])
        self.assertEqual([call[0] for call in indexer.calls], ["bm25"])

    def test_authorization_and_embedding_run_in_parallel(self) -> None:
        barrier = threading.Barrier(2)
        service = RAGRetrievalService(
            repository=InMemoryRagMVPRepository(),
            indexer=_Indexer(),
            embedding=_BarrierEmbedding(barrier),
            authorization=_BarrierAuthorization(barrier),
        )

        response = service.search(SearchRequest(
            query="并行检索",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="global",
        ))

        self.assertEqual(response.diagnostics["effective_execution_path"], "traditional")

    def test_bm25_and_knn_run_in_parallel(self) -> None:
        barrier = threading.Barrier(2)
        service = RAGRetrievalService(
            repository=InMemoryRagMVPRepository(),
            indexer=_BarrierIndexer(barrier),
            embedding=_Embedding(),
            authorization=_Authorization(),
        )

        response = service.search(SearchRequest(
            query="并行召回",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="global",
        ))

        self.assertEqual(response.diagnostics["effective_execution_path"], "traditional")

    def test_the_tree_entry_reports_no_match_instead_of_falling_back(self) -> None:
        # Empty registry: the tree surface has nothing to narrow to. It says so
        # and returns nothing - the caller decides whether to ask for
        # traditional retrieval next.
        repository = InMemoryRagMVPRepository()
        indexer = _Indexer()
        original = settings.tree_mode
        object.__setattr__(settings, "tree_mode", "tree")
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
                entry="tree",
            ))
        finally:
            object.__setattr__(settings, "tree_mode", original)

        self.assertEqual(list(response.results), [])
        self.assertEqual(response.diagnostics["channel_policy"], "tree")
        self.assertEqual(response.diagnostics["effective_execution_path"], "tree")
        self.assertEqual(response.diagnostics["fallback_reason"], "no_entity_match")
        # Not one search was issued: there is no scope to search in, and the
        # unscoped fallback is not this surface's job.
        self.assertEqual(indexer.calls, [])

    def test_the_effective_mount_threshold_reaches_the_indexer(self) -> None:
        # The caller left min_mount_confidence unset, so the configured tree
        # mount floor is the effective value. Passing the raw request field through
        # would leave the ES filter with None and let every mount in,
        # regardless of confidence.
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
        object.__setattr__(settings, "tree_mode", "tree")
        try:
            service = RAGRetrievalService(
                repository=repository,
                indexer=indexer,
                embedding=_Embedding(),
                authorization=_Authorization(),
            )
            service.search(SearchRequest(
                query="青云项目进展",
                user_id="user-1",
                scope_type="organization",
                scope_id="org-1",
                knowledge_base_ids=("kb-1",),
                entry="tree",
            ))
        finally:
            object.__setattr__(settings, "tree_mode", original)

        self.assertTrue(indexer.requests)
        self.assertEqual(
            {request.min_mount_confidence for request in indexer.requests},
            {settings.tree_min_mount_confidence},
        )

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

    def test_scope_export_uses_offset_and_stable_sort(self) -> None:
        client = _RecordingElasticsearch()
        index = RagChunkIndex(client)
        request = SearchRequest(
            query="",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="export",
            top_k=50,
            offset=50,
            resource_types=("message",),
        )

        index.search_bm25(request, size=request.top_k)

        self.assertTrue(client.calls)
        self.assertTrue(all(call.get("from_") == 50 for call in client.calls))
        self.assertTrue(all(call.get("size") == 50 for call in client.calls))
        self.assertTrue(
            all(
                call.get("sort")
                and call["sort"][0]["sent_at"]["order"] == "desc"
                for call in client.calls
            )
        )

    def test_context_scope_records_under_the_production_signature(self) -> None:
        # Regression: this call was missing tree_mode / execution_path /
        # diagnostics / duration_ms / request_id. The strict Postgres
        # record_search raised TypeError, the caller swallowed it, and the read
        # was never recorded in production - while the loose in-memory double
        # kept the suite green. The strict double fails loudly instead.
        repository = _StrictRecordSearchRepository()
        service = RAGRetrievalService(
            repository=repository,
            indexer=_Indexer(),
            embedding=_Embedding(),
            authorization=_Authorization(),
        )

        response = service.context_scope(
            SearchRequest(
                query="",
                user_id="user-1",
                scope_type="organization",
                scope_id="org-1",
                entry="context",
            ),
            anchors=[{"conversation_id": "conv-1", "sent_at": "2026-10-01T00:00:00Z"}],
            radius=3,
        )

        self.assertEqual(
            response.diagnostics["effective_execution_path"], "context_scope"
        )
        recorded = repository.searches
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["execution_path"], "context_scope")
        self.assertEqual(recorded[0]["tree_mode"], settings.tree_mode)
        self.assertEqual(recorded[0]["result_count"], len(response.results))
        self.assertGreaterEqual(recorded[0]["duration_ms"], 0)
        self.assertTrue(recorded[0]["request_id"])
        self.assertEqual(recorded[0]["diagnostics"]["radius"], 3)

    def test_the_strict_double_rejects_an_incomplete_recording(self) -> None:
        # Self-check on the guard: if it accepted anything, the regression tests
        # beside it would pass for the wrong reason.
        repository = _StrictRecordSearchRepository()

        with self.assertRaises(AssertionError):
            repository.record_search(user_id="user-1", scope_type="organization")

    def test_export_scope_records_under_the_production_signature(self) -> None:
        repository = _StrictRecordSearchRepository()
        service = RAGRetrievalService(
            repository=repository,
            indexer=_Indexer(),
            embedding=_Embedding(),
            authorization=_Authorization(),
        )

        service.export_scope(SearchRequest(
            query="",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="export",
            top_k=50,
            offset=50,
            sender_ids=("sender-1",),
            resource_types=("message",),
        ))

        self.assertEqual(
            [row["execution_path"] for row in repository.searches], ["scope_export"]
        )

    def test_export_scope_returns_complete_page_diagnostics(self) -> None:
        indexer = _Indexer()
        service = RAGRetrievalService(
            repository=InMemoryRagMVPRepository(),
            indexer=indexer,
            embedding=_Embedding(),
            authorization=_Authorization(),
        )
        response = service.export_scope(SearchRequest(
            query="",
            user_id="user-1",
            scope_type="organization",
            scope_id="org-1",
            entry="export",
            top_k=50,
            offset=50,
            sender_ids=("sender-1",),
            resource_types=("message",),
        ))
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.diagnostics["offset"], 50)
        self.assertEqual(response.diagnostics["effective_execution_path"], "scope_export")

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

    def test_scoped_filters_stay_a_flat_list_without_a_threshold(self) -> None:
        request = SearchRequest(
            query="青云项目", user_id="user-1",
            scope_type="organization", scope_id="org-1",
        )

        filters = _filters(request, entity_ids=("e1",))

        self.assertIn({"terms": {"entity_ids": ["e1"]}}, filters)
        self.assertFalse([item for item in filters if "nested" in item])

    def test_a_mount_threshold_switches_to_a_nested_filter(self) -> None:
        # A flat keyword list cannot express "this entity *and* at least this
        # confidence", so the threshold has to be asked of the mount documents.
        request = SearchRequest(
            query="青云项目", user_id="user-1",
            scope_type="organization", scope_id="org-1",
            min_mount_confidence=0.85,
        )

        filters = _filters(request, entity_ids=("e1", "e2"))

        nested = [item for item in filters if "nested" in item]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["nested"]["path"], "entity_mounts")
        clauses = nested[0]["nested"]["query"]["bool"]["filter"]
        self.assertIn(
            {"terms": {"entity_mounts.entity_id": ["e1", "e2"]}}, clauses
        )
        self.assertIn(
            {"range": {"entity_mounts.confidence": {"gte": 0.85}}}, clauses
        )
        # The flat list is replaced, not kept alongside: the nested filter
        # already requires the entity and adds the confidence bound.
        self.assertNotIn({"terms": {"entity_ids": ["e1", "e2"]}}, filters)

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
