from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.application.entity_locator import EntityLocator
from app.application.tree_rollout import (
    parse_rollout_scopes,
    resolve_tree_mode,
    shadow_sampled,
)
from app.application.mvp_ports import (
    AuthorizationGateway,
    EmbeddingProvider,
    SearchIndexer,
)
from app.config import settings
from app.domain.rag import AccessCheck, AuthorizationScope, SearchRequest, SearchResult
from app.domain.location import EntityScope, LocateRequest, LocateResult


logger = logging.getLogger("rag.retrieval")


class SearchUnavailable(RuntimeError):
    pass


class AuthorizationUnavailable(RuntimeError):
    pass


class AuthorizationDenied(RuntimeError):
    pass


@dataclass
class RetrievalResponse:
    request_id: str
    results: list[SearchResult]
    diagnostics: dict[str, Any]


# Which retrieval channels an entry point is allowed to use.
#
# The split is by entry, not by a deployment-wide switch. The global search box
# and the agent's content search must stay plain hybrid retrieval (bm25 + knn
# over the whole scope); the tree is a surface the caller opts into by calling
# /search/tree. Because a request resolves to exactly one policy, the unscoped
# and entity-scoped channels never share a fusion step - that is what "keep the
# tree separate from traditional RAG" means in code.
#
# Falling back is deliberately NOT done here: when the tree has nothing to
# offer, /search/tree returns nothing and a reason. Deciding to ask for
# traditional retrieval next belongs to the caller (the agent's tool loop).
TREE_CHANNEL_POLICY = "tree"
DEFAULT_CHANNEL_POLICY = "hybrid"


def channel_policy_for(entry: str) -> str:
    return TREE_CHANNEL_POLICY if str(entry or "") == "tree" else DEFAULT_CHANNEL_POLICY

class RAGRetrievalService:
    def __init__(
        self,
        *,
        repository: object,
        indexer: SearchIndexer,
        embedding: EmbeddingProvider,
        authorization: AuthorizationGateway,
        answer_provider: Any | None = None,
        verifier: Any | None = None,
    ) -> None:
        self.repository = repository
        self.indexer = indexer
        self.embedding = embedding
        self.authorization = authorization
        self.answer_provider = answer_provider
        # L4 is optional: without a verifier the locator simply never escalates,
        # which keeps retrieval working when no LLM is configured.
        self.locator = EntityLocator(
            repository=repository, embedding=embedding, verifier=verifier
        )

    def search(self, request: SearchRequest) -> RetrievalResponse:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        request = _resolve_time_request(request)
        scope = self.authorization.search_scope(
            user_id=request.user_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            resource_parts=("original", "content"),
        )
        protected_keys = scope.authorized_protected_object_keys if scope.available else ()
        if scope.denied:
            raise AuthorizationDenied("authorization denied")
        if scope.failed or scope.truncated:
            raise AuthorizationUnavailable("authorization scope is incomplete")
        if not scope.available:
            request = replace(request, include_protected=False)
            protected_keys = ()
        # Channel policy for this entry, then the rollout state of the tree
        # surface. Traditional entries never call the locator at all.
        policy = channel_policy_for(request.entry)
        tree_mode = resolve_tree_mode(
            default=settings.tree_mode,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            rollout_scopes=parse_rollout_scopes(settings.tree_rollout_scopes),
        )
        shadow_sample = True
        tree_reason: str | None = None
        locate = None
        entity_ids: tuple[str, ...] = ()
        if policy == TREE_CHANNEL_POLICY:
            if tree_mode == "off":
                tree_reason = "tree_disabled"
            else:
                if tree_mode == "shadow":
                    shadow_sample = shadow_sampled(
                        scope_type=request.scope_type,
                        scope_id=request.scope_id,
                        query=request.query,
                        sample_rate=settings.tree_shadow_sample_rate,
                    )
                if tree_mode == "shadow" and not shadow_sample:
                    tree_reason = "shadow_not_sampled"
                else:
                    locate = self._locate(request, tree_mode=tree_mode, shadow_sample=True)
                    entity_ids = locate.scope.entity_ids if locate is not None else ()
                    if not entity_ids:
                        tree_reason = "no_entity_match"
        # The unscoped channels run for traditional entries, and for the shadow
        # phase where the tree is measured but its result is not adopted yet.
        run_unscoped = policy != TREE_CHANNEL_POLICY or tree_mode == "shadow"
        run_tree_channels = (
            policy == TREE_CHANNEL_POLICY
            and tree_mode in {"shadow", "tree"}
            and bool(entity_ids)
        )
        # Strip resolved entity names out of the retrieval text: once the entity
        # is known, the name only biases BM25 towards chunks that repeat it.
        retrieval_request = request
        if locate is not None and locate.scope.residual_query:
            retrieval_request = replace(request, query=locate.scope.residual_query)
        if locate is not None and locate.scope.min_mount_confidence:
            # The locator resolves the effective threshold (request value, else
            # configuration). Hand that downstream instead of the raw request
            # field, otherwise a caller who left it unset would get every mount
            # regardless of confidence.
            retrieval_request = replace(
                retrieval_request,
                min_mount_confidence=locate.scope.min_mount_confidence,
            )
        query_vector: list[float] | None = None
        degraded: list[str] = []
        if request.entry not in {"knowledge", "sources"}:
            try:
                vectors = self.embedding.embed([retrieval_request.query])
                query_vector = vectors[0] if vectors else None
            except Exception:
                degraded.append("embedding_failed")
        global_branches: dict[str, list[SearchResult]] = {}
        # Only the tree entry in tree mode skips this: the whole point of the
        # split is that a tree request does not pay for an unscoped query it is
        # not going to use.
        if run_unscoped:
            try:
                global_branches["bm25"] = _annotate_branch(
                    "bm25",
                    self.indexer.search_bm25(
                        retrieval_request,
                        protected_object_keys=protected_keys,
                    ),
                )
                if query_vector is not None:
                    global_branches["knn"] = _annotate_branch(
                        "knn",
                        self.indexer.search_knn(
                            retrieval_request,
                            query_vector,
                            protected_object_keys=protected_keys,
                        ),
                    )
            except Exception as exc:
                raise SearchUnavailable("Elasticsearch retrieval failed") from exc
        scoped_branches: dict[str, list[SearchResult]] = {}
        if run_tree_channels:
            try:
                scoped_branches["branch_bm25"] = _annotate_branch(
                    "bm25",
                    self.indexer.search_bm25(
                        retrieval_request,
                        entity_ids=entity_ids,
                        protected_object_keys=protected_keys,
                    ),
                )
                if query_vector is not None:
                    scoped_branches["branch_knn"] = _annotate_branch(
                        "knn",
                        self.indexer.search_knn(
                            retrieval_request,
                            query_vector,
                            entity_ids=entity_ids,
                            protected_object_keys=protected_keys,
                        ),
                    )
            except Exception:
                degraded.append("branch_failed")
                scoped_branches = {}
        effective, execution_path, fallback_reason = select_retrieval_channels(
            policy=policy,
            tree_mode=tree_mode,
            global_branches=global_branches,
            scoped_branches=scoped_branches,
            tree_reason=tree_reason,
            degraded=degraded,
        )
        related_branches: dict[str, list[SearchResult]] = {}
        related_entities: list[dict[str, Any]] = []
        if run_tree_channels:
            # Lateral move between nodes: still the tree surface, never a way
            # back into the unscoped corpus.
            related_branches, related_entities = self._expand_relations(
                request=request,
                retrieval_request=retrieval_request,
                entity_ids=entity_ids,
                scoped_branches=scoped_branches,
                query_vector=query_vector,
                protected_keys=protected_keys,
                degraded=degraded,
                tree_mode=tree_mode,
            )
            if related_branches:
                effective.update(related_branches)
        raw_global_candidate_count = sum(len(value) for value in global_branches.values())
        raw_branch_candidate_count = sum(len(value) for value in scoped_branches.values())
        raw_related_candidate_count = sum(len(value) for value in related_branches.values())
        gate_dropped = 0
        if request.entry == "ai":
            effective, gate_dropped = filter_qa_anchor_candidates(
                effective,
                vector_min_score=settings.qa_vector_min_score,
                bm25_min_score=settings.qa_bm25_min_score,
            )
        fused = rrf_fuse(
            effective,
            k=settings.rrf_k,
            weights={
                "bm25": settings.rrf_keyword_weight,
                "knn": settings.rrf_vector_weight,
                "branch_bm25": settings.tree_mount_weight,
                "branch_knn": settings.tree_mount_weight,
                "related_bm25": settings.tree_relation_weight,
                "related_knn": settings.tree_relation_weight,
            },
        )
        fused = dedupe_logical_positions(fused)
        if request.entry == "sources":
            anchor_max_per_resource = 1
            final_max_per_resource = 1
        elif request.entry == "content":
            anchor_max_per_resource = 3
            final_max_per_resource = 3
        else:
            anchor_max_per_resource = max(1, settings.anchors_per_item)
            final_max_per_resource = max(1, settings.max_chunks_per_item)
        anchor_candidates = select_anchors(
            fused,
            max_per_resource=anchor_max_per_resource,
            limit=max(request.top_k * 3, anchor_max_per_resource),
        )
        authorized_anchors = self._authorize_results(request, anchor_candidates, scope)
        authorized_anchors = self._upgrade_protected_variants(
            request, authorized_anchors, scope
        )
        neighbors: list[SearchResult] = []
        expandable_anchors = [
            item for item in authorized_anchors
            if item.source.get("resource_type") == "attachment"
        ]
        if settings.neighbor_radius > 0 and expandable_anchors:
            try:
                neighbors = self.indexer.search_neighbors(
                    request,
                    expandable_anchors,
                    protected_object_keys=protected_keys,
                    radius=settings.neighbor_radius,
                )
            except Exception:
                degraded.append("neighbor_expansion_failed")
        authorized_neighbors = self._authorize_results(request, neighbors, scope)
        results = finalize_resource_chunks(
            authorized_anchors,
            authorized_neighbors,
            limit=request.top_k * final_max_per_resource,
            max_per_resource=final_max_per_resource,
        )
        diagnostics = {
            # Which surface this request used. Recorded next to tree_mode so a
            # "the tree changed my search box" report can be answered from data.
            "channel_policy": policy,
            "tree_mode": tree_mode,
            # Kept alongside the resolved mode so a rollout can be audited later:
            # "why did this scope run tree?" has to be answerable after the fact.
            "tree_mode_default": settings.tree_mode,
            "tree_shadow_sampled": shadow_sample,
            "authorization_snapshot_id": scope.snapshot_id,
            "authorized_protected_object_count": len(protected_keys),
            "scope_truncated": scope.truncated,
            "resolved_entity_count": len(entity_ids),
            "entity_scope": {
                "entity_ids": list(entity_ids),
                "composition": locate.scope.composition if locate else None,
                "residual_query": retrieval_request.query if locate else None,
                "unresolved": list(locate.scope.unresolved) if locate else [],
            },
            "locate": locate.diagnostics if locate else None,
            # Lifted out of the per-mention trace so the plan's "常规路径定位
            # p95 <= 80ms" gate can be read as a number instead of eyeballed.
            "locate_ms": float((locate.diagnostics or {}).get("locate_ms") or 0.0)
            if locate
            else 0.0,
            "locate_layer_ms": (locate.diagnostics or {}).get("layer_ms")
            if locate
            else None,
            # Lifted out of the per-mention trace so the L4 call rate is a single
            # queryable flag rather than nested JSON.
            "locate_llm_invoked": bool(
                locate
                and any(
                    item.get("llm_invoked")
                    for item in (locate.diagnostics.get("mentions") or [])
                )
            ),
            "branch_candidate_count": raw_branch_candidate_count,
            "related_entity_count": len(related_entities),
            "related_candidate_count": raw_related_candidate_count,
            "related_entities": [
                {
                    "entity_id": item["entity_id"],
                    "relation_type": item["relation_type"],
                    "confidence": item["confidence"],
                    # Bounded: the full chain can grow across many windows and
                    # this blob is written to search_history on every query. A
                    # truncated list still answers "was the model alone here?".
                    "evidence_chunk_ids": list(item.get("evidence_chunk_ids") or [])[:5],
                }
                for item in related_entities
            ],
            "global_candidate_count": raw_global_candidate_count,
            "score_gate_dropped_count": gate_dropped,
            "anchor_candidate_count": len(anchor_candidates),
            "authorization_candidate_count": len(anchor_candidates),
            "neighbor_candidate_count": len(neighbors),
            "fused_result_count": len(results),
            "retrieval_settings": {
                "anchors_per_item": settings.anchors_per_item,
                "max_chunks_per_item": settings.max_chunks_per_item,
                "neighbor_radius": settings.neighbor_radius,
                "qa_vector_min_score": settings.qa_vector_min_score,
                "qa_bm25_min_score": settings.qa_bm25_min_score,
                "bm25_top_k": settings.bm25_top_k,
                "knn_top_k": settings.knn_top_k,
            },
            "effective_execution_path": (
                "metadata_filter" if request.entry == "sources" else execution_path
            ),
            "fallback_reason": None if request.entry == "sources" else fallback_reason,
            "degraded_reason": ",".join(degraded) if degraded else None,
        }
        try:
            self.repository.record_search(
                user_id=request.user_id,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                query_hash=hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
                query_redacted=redact_query(request.query),
                filters={
                    "entry": request.entry,
                    "knowledge_base_ids": list(request.knowledge_base_ids),
                },
                tree_mode=tree_mode,
                execution_path=diagnostics["effective_execution_path"],
                diagnostics=diagnostics,
                result_count=len(results),
                duration_ms=int((time.perf_counter() - started) * 1000),
                request_id=request_id,
            )
        except Exception:
            # Search must not fail because history could not be written, but a
            # silent pass hid a check-constraint mismatch that dropped every
            # tree-mode row for a whole release. Log it so it is visible.
            logger.warning("search history was not recorded", exc_info=True)
        return RetrievalResponse(request_id=request_id, results=results, diagnostics=diagnostics)

    def context_scope(
        self,
        request: SearchRequest,
        *,
        anchors: list[dict[str, Any]],
        radius: int = 2,
    ) -> RetrievalResponse:
        """Messages around the caller's anchors, in the same conversation.

        The anchor filters (sender / conversation / phrase) describe *which*
        messages were found, not which neighbours are allowed, so they are
        dropped here; the anchor's own conversation plus per-result
        authorization decide what comes back.
        """

        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        scope = self.authorization.search_scope(
            user_id=request.user_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            resource_parts=("original", "content"),
        )
        if scope.denied:
            raise AuthorizationDenied("authorization denied")
        if scope.failed or scope.truncated:
            raise AuthorizationUnavailable("authorization scope is incomplete")
        protected_keys: tuple[str, ...] = ()
        if scope.available:
            protected_keys = scope.authorized_protected_object_keys
        else:
            request = replace(request, include_protected=False)
        window = replace(
            request,
            query="",
            sender_ids=(),
            sender_names=(),
            conversation_ids=(),
            conversation_names=(),
            content_contains=(),
            resource_ids=(),
            resource_types=("message",),
            offset=0,
        )
        try:
            values = self.indexer.search_message_context(
                window, anchors, radius=radius
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a service error
            raise SearchUnavailable("Elasticsearch retrieval failed") from exc
        results = self._authorize_results(request, values, scope)
        diagnostics = {
            "authorization_snapshot_id": scope.snapshot_id,
            "scope_truncated": scope.truncated,
            "anchor_count": len(anchors),
            "radius": radius,
            "returned_chunk_count": len(results),
            "effective_execution_path": "context_scope",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        try:
            self.repository.record_search(
                user_id=request.user_id,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                query_hash=hashlib.sha256(b"context").hexdigest(),
                query_redacted="",
                filters={
                    "entry": request.entry,
                    "anchors": len(anchors),
                    "radius": radius,
                },
                # The strict Postgres signature needs every one of these. They
                # were missing here, and the except below swallowed the
                # TypeError: the read was never recorded in production while the
                # in-memory double (whose record_search takes **value) made the
                # tests look green.
                tree_mode=settings.tree_mode,
                execution_path=diagnostics["effective_execution_path"],
                diagnostics=diagnostics,
                result_count=len(results),
                duration_ms=int((time.perf_counter() - started) * 1000),
                request_id=request_id,
            )
        except Exception:  # noqa: BLE001 - audit must not break retrieval
            logger.warning("context scope read was not recorded", exc_info=True)
        return RetrievalResponse(
            request_id=request_id, results=results, diagnostics=diagnostics
        )

    def export_scope(self, request: SearchRequest) -> RetrievalResponse:
        """Return complete chunks for one metadata-defined scope page.

        This is the agent's "take the whole person scope" primitive: it keeps
        the authorization boundary and metadata filters, but skips BM25/KNN
        ranking. Sorting is deterministic (sent_at, chunk_id) so the caller can
        page with ``offset`` until ``has_more`` is false without silently
        dropping the few messages that happen to rank low for a weak query.
        """

        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        request = _resolve_time_request(request)
        scope = self.authorization.search_scope(
            user_id=request.user_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            resource_parts=("original", "content"),
        )
        if scope.denied:
            raise AuthorizationDenied("authorization denied")
        if scope.failed or scope.truncated:
            raise AuthorizationUnavailable("authorization scope is incomplete")
        if scope.available:
            protected_keys = scope.authorized_protected_object_keys
        else:
            request = replace(request, include_protected=False)
            protected_keys = ()
        try:
            values = self.indexer.search_bm25(
                request,
                protected_object_keys=protected_keys,
                size=request.top_k,
            )
        except Exception as exc:
            raise SearchUnavailable("Elasticsearch retrieval failed") from exc
        results = self._authorize_results(request, values, scope)
        diagnostics = {
            "authorization_snapshot_id": scope.snapshot_id,
            "scope_truncated": scope.truncated,
            "offset": request.offset,
            "page_size": request.top_k,
            "returned_chunk_count": len(results),
            "effective_execution_path": "scope_export",
        }
        try:
            self.repository.record_search(
                user_id=request.user_id,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                query_hash=hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
                query_redacted=redact_query(request.query),
                filters={
                    "entry": request.entry,
                    "offset": request.offset,
                    "resource_ids": list(request.resource_ids),
                },
                tree_mode=settings.tree_mode,
                execution_path=diagnostics["effective_execution_path"],
                diagnostics=diagnostics,
                result_count=len(results),
                duration_ms=int((time.perf_counter() - started) * 1000),
                request_id=request_id,
            )
        except Exception:  # noqa: BLE001 - audit must not break the export
            logger.warning("scope export was not recorded", exc_info=True)
        return RetrievalResponse(
            request_id=request_id,
            results=results,
            diagnostics=diagnostics,
        )

    def answer(self, request: SearchRequest) -> dict[str, Any]:
        response = self.search(request)
        provider = self.answer_provider
        if provider is None:
            from app.infrastructure.qa import OpenAICompatibleAnswerProvider

            provider = OpenAICompatibleAnswerProvider()
        question = request.query
        answer = provider.generate(question, response.results)
        citations = [_citation(result, index) for index, result in enumerate(response.results, start=1)]
        return {
            "request_id": response.request_id,
            "answer": answer,
            "items": [result.safe_dict() for result in response.results],
            "citations": citations,
            "diagnostics": response.diagnostics,
            "retrieval_mode": request.qa_mode,
            "execution_path": response.diagnostics["effective_execution_path"],
        }

    def _expand_relations(
        self,
        *,
        request: SearchRequest,
        retrieval_request: SearchRequest,
        entity_ids: tuple[str, ...],
        scoped_branches: dict[str, list[SearchResult]],
        query_vector: list[float] | None,
        protected_keys: tuple[str, ...],
        degraded: list[str],
        tree_mode: str | None = None,
    ) -> tuple[dict[str, list[SearchResult]], list[dict[str, Any]]]:
        """Widen to one hop of neighbours when the entity's own node is thin.

        Falling straight back to full-corpus search throws away the fact that we
        know which entity the user meant; one hop keeps that context while still
        reaching "张三参与的项目" style questions.
        """
        if not settings.tree_relation_expansion_enabled:
            return {}, []
        if (tree_mode or settings.tree_mode) != "tree":
            return {}, []
        if not entity_ids or not scoped_branches:
            return {}, []
        scoped_count = sum(len(values) for values in scoped_branches.values())
        if scoped_count >= max(1, int(request.top_k * 0.5)):
            return {}, []
        try:
            related = self.repository.find_related_entities(
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                entity_ids=list(entity_ids),
                direction="both",
                min_confidence=settings.tree_relation_min_confidence,
                limit=settings.tree_relation_max_entities,
            )
        except Exception:
            degraded.append("relation_lookup_failed")
            return {}, []
        related_ids = tuple(dict.fromkeys(item["entity_id"] for item in related))
        if not related_ids:
            return {}, []
        branches: dict[str, list[SearchResult]] = {}
        try:
            branches["related_bm25"] = _annotate_branch(
                "bm25",
                self.indexer.search_bm25(
                    retrieval_request,
                    entity_ids=related_ids,
                    protected_object_keys=protected_keys,
                ),
            )
            if query_vector is not None:
                branches["related_knn"] = _annotate_branch(
                    "knn",
                    self.indexer.search_knn(
                        retrieval_request,
                        query_vector,
                        entity_ids=related_ids,
                        protected_object_keys=protected_keys,
                    ),
                )
        except Exception:
            degraded.append("relation_search_failed")
            return {}, []
        for values in branches.values():
            for result in values:
                result.source["retrieval_origin"] = "related"
        return branches, related

    def _locate(
        self,
        request: SearchRequest,
        *,
        tree_mode: str | None = None,
        shadow_sample: bool = True,
    ) -> LocateResult | None:
        """Resolve the request's entities, or None when the tree does not apply.

        The `sources` entry point addresses a specific knowledge item rather
        than an entity, so it deliberately skips location. `tree_mode` is the
        resolved per-request mode; it defaults to the deployment setting for
        callers that bypass the rollout control.
        """
        mode = tree_mode or settings.tree_mode
        if mode == "off" or request.entry == "sources":
            return None
        if mode == "shadow" and not shadow_sample:
            # Sampling exists to keep shadow mode affordable. This request was
            # not picked, so it skips the locate call entirely; the diagnostics
            # flag it so the quality rates do not count a deliberate skip as a
            # location miss.
            return None
        if request.entity_ids:
            # The Agent already resolved entities while planning; reusing them
            # keeps multi-step plans consistent and skips L0-L4 entirely.
            return LocateResult(
                entities=(),
                scope=EntityScope(
                    entity_ids=tuple(request.entity_ids)[: settings.tree_max_entities],
                    composition=request.entity_composition or "and",
                    min_mount_confidence=(
                        request.min_mount_confidence
                        if request.min_mount_confidence is not None
                        else settings.tree_min_mount_confidence
                    ),
                    residual_query=request.query,
                ),
                diagnostics={"source": "request"},
            )
        return self.locator.locate(
            LocateRequest(
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                query=request.query,
                conversation_id=request.conversation_id,
                occurred_after=request.occurred_after,
                occurred_before=request.occurred_before,
                top_k=request.top_k,
                min_mount_confidence=(
                    request.min_mount_confidence
                    if request.min_mount_confidence is not None
                    else settings.tree_min_mount_confidence
                ),
                allow_llm=request.locate_allow_llm,
            )
        )

    def _authorize_branches(
        self,
        request: SearchRequest,
        branches: dict[str, list[SearchResult]],
        scope: AuthorizationScope,
    ) -> dict[str, list[SearchResult]]:
        all_results = [item for values in branches.values() for item in values]
        allowed = self._authorize_results(request, all_results, scope)
        allowed_ids = {item.chunk_id for item in allowed}
        return {
            name: [item for item in values if item.chunk_id in allowed_ids]
            for name, values in branches.items()
        }

    def _authorize_results(
        self,
        request: SearchRequest,
        values: list[SearchResult],
        scope: AuthorizationScope,
    ) -> list[SearchResult]:
        values = [item for item in values if str(item.source.get("lifecycle_status") or "active") == "active"]
        checks = [_access_check(item) for item in values]
        if not checks:
            return []
        decisions = self.authorization.check_batch(
            user_id=request.user_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            checks=checks,
            snapshot_id=scope.snapshot_id,
        )
        if len(decisions) != len(values):
            return []
        return [item for item, allowed in zip(values, decisions) if allowed]

    def _upgrade_protected_variants(
        self,
        request: SearchRequest,
        values: list[SearchResult],
        scope: AuthorizationScope,
    ) -> list[SearchResult]:
        """Replace display hits with the protected original when allowed.

        The protected-object list from Core is an optimization, not a source of
        truth: OpenFGA can return a capped, unstable subset. Authorization is
        therefore decided per candidate with an exact check, and only then is
        the protected chunk loaded and swapped in.
        """

        if not request.include_protected or not values:
            return values
        fetch = getattr(self.indexer, "search_protected_variants", None)
        if not callable(fetch):
            return values
        item_ids = tuple(
            dict.fromkeys(
                str(item.source.get("knowledge_item_id") or "")
                for item in values
                if item.source.get("content_variant") != "protected"
            )
        )
        item_ids = tuple(value for value in item_ids if value)
        if not item_ids:
            return values
        checks = [
            AccessCheck("knowledge_item", "original", item_id, "view")
            for item_id in item_ids
        ]
        try:
            decisions = self.authorization.check_batch(
                user_id=request.user_id,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                checks=checks,
                snapshot_id=scope.snapshot_id,
            )
        except Exception:
            return values
        if len(decisions) != len(item_ids):
            return values
        allowed_ids = {
            item_id for item_id, allowed in zip(item_ids, decisions) if allowed
        }
        if not allowed_ids:
            return values
        try:
            protected = fetch(request, tuple(allowed_ids))
        except Exception:
            return values
        if not protected:
            return values

        selected: dict[str, SearchResult] = {}
        order: list[str] = []
        for item in values:
            key = str(item.source.get("logical_position_key") or item.chunk_id)
            if key not in selected:
                order.append(key)
            selected[key] = item
        for item in protected:
            knowledge_item_id = str(item.source.get("knowledge_item_id") or "")
            if knowledge_item_id not in allowed_ids:
                continue
            key = str(item.source.get("logical_position_key") or item.chunk_id)
            if key not in selected:
                order.append(key)
            selected[key] = item
        return [selected[key] for key in order]


def _access_check(result: SearchResult) -> AccessCheck:
    source = result.source
    protected = source.get("content_variant") == "protected"
    resource_type = str(source.get("resource_type") or "message")
    resource_id = str(source.get("resource_id") or "")
    if resource_type == "attachment":
        return AccessCheck("attachment", "content", resource_id, "view")
    if protected:
        return AccessCheck("knowledge_item", "original", str(source.get("knowledge_item_id") or ""), "view")
    return AccessCheck("knowledge_item", "display", str(source.get("knowledge_item_id") or ""), "view")


def _annotate_branch(branch: str, results: list[SearchResult]) -> list[SearchResult]:
    for rank, result in enumerate(results, start=1):
        result.rank = rank
        if branch == "bm25":
            result.source["bm25_score"] = result.score
            result.source["bm25_rank"] = rank
        elif branch == "knn":
            result.source["vector_score"] = result.score
            result.source["vector_rank"] = rank
        matched = {
            str(value)
            for value in result.source.get("matched_by", ())
            if str(value)
        }
        matched.add(branch)
        result.source["matched_by"] = sorted(matched)
    return results


def select_retrieval_channels(
    *,
    policy: str,
    tree_mode: str,
    global_branches: dict[str, list[SearchResult]],
    scoped_branches: dict[str, list[SearchResult]],
    tree_reason: str | None,
    degraded: list[str],
) -> tuple[dict[str, list[SearchResult]], str, str | None]:
    """Pick the channels for one request. Never both at once.

    Traditional entries return the unscoped result. The tree entry returns the
    entity-scoped result, or nothing plus a reason - it does **not** substitute
    the unscoped result, because "should I fall back?" belongs to the caller
    (the agent's tool loop), not to the retrieval service.

    This replaces the earlier tree-first fusion, which put the entity-scoped and
    the unscoped channels into one RRF pass. That made the search box's result
    depend on the tree, which is exactly what the entry split removes.

    `shadow` is the validation phase in between: the tree runs and its
    diagnostics are recorded, but the result stays the traditional one, so
    nothing depends on the tree being good yet.
    """
    if policy != TREE_CHANNEL_POLICY:
        return dict(global_branches), "traditional", None
    if tree_mode == "shadow":
        return dict(global_branches), "tree_shadow", tree_reason
    if tree_reason:
        return {}, "tree", tree_reason
    if "branch_failed" in degraded:
        return {}, "tree", "branch_failed"
    if not scoped_branches:
        return {}, "tree", "empty_scope"
    return dict(scoped_branches), "tree", None


def filter_qa_anchor_candidates(
    branches: dict[str, list[SearchResult]],
    *,
    vector_min_score: float,
    bm25_min_score: float,
) -> tuple[dict[str, list[SearchResult]], int]:
    output: dict[str, list[SearchResult]] = {}
    dropped = 0
    for name, results in branches.items():
        kept: list[SearchResult] = []
        for result in results:
            source = result.source
            bm25_score = _float_or_none(source.get("bm25_score"))
            vector_score = _float_or_none(source.get("vector_score"))
            bm25_ok = bm25_score is not None and (
                bm25_score >= bm25_min_score
                if bm25_min_score > 0
                else bm25_score > 0
            )
            vector_ok = vector_score is not None and vector_score >= vector_min_score
            if bm25_ok or vector_ok:
                kept.append(result)
            else:
                dropped += 1
        if kept:
            output[name] = kept
    return output, dropped


def select_anchors(
    results: list[SearchResult],
    *,
    max_per_resource: int,
    limit: int,
) -> list[SearchResult]:
    counts: dict[str, int] = {}
    output: list[SearchResult] = []
    for result in results:
        key = _resource_key(result)
        if counts.get(key, 0) >= max(1, max_per_resource):
            continue
        counts[key] = counts.get(key, 0) + 1
        result.source["retrieval_role"] = "anchor"
        result.rank = len(output) + 1
        output.append(result)
        if len(output) >= max(1, limit):
            break
    return output


def finalize_resource_chunks(
    anchors: list[SearchResult],
    neighbors: list[SearchResult],
    *,
    limit: int,
    max_per_resource: int,
) -> list[SearchResult]:
    anchor_order: list[str] = []
    groups: dict[str, dict[str, list[SearchResult]]] = {}
    for anchor in anchors:
        key = _resource_key(anchor)
        if key not in groups:
            groups[key] = {"anchors": [], "neighbors": []}
            anchor_order.append(key)
        groups[key]["anchors"].append(anchor)
    for neighbor in neighbors:
        key = _resource_key(neighbor)
        if key in groups:
            groups[key]["neighbors"].append(neighbor)

    output: list[SearchResult] = []
    for key in anchor_order:
        group = groups[key]
        anchor_values = sorted(group["anchors"], key=lambda item: item.rank)
        anchor_ids = {item.chunk_id for item in anchor_values}
        selected = {item.chunk_id: item for item in anchor_values}
        anchor_positions = {
            _chunk_index(item)
            for item in anchor_values
            if _chunk_index(item) is not None
        }
        candidates = [
            item for item in group["neighbors"]
            if item.chunk_id not in anchor_ids
        ]
        candidates.sort(
            key=lambda item: (
                min(
                    (
                        abs((_chunk_index(item) or 0) - anchor_index)
                        for anchor_index in anchor_positions
                    ),
                    default=10**6,
                ),
                _chunk_index(item) if _chunk_index(item) is not None else 10**6,
                item.chunk_id,
            )
        )
        for neighbor in candidates:
            if len(selected) >= max(1, max_per_resource):
                break
            nearest = min(
                anchor_values,
                key=lambda item: abs(
                    (_chunk_index(item) or 0) - (_chunk_index(neighbor) or 0)
                ),
            )
            neighbor.source["retrieval_role"] = "neighbor"
            neighbor.source["neighbor_of_chunk_id"] = nearest.chunk_id
            neighbor.score = nearest.score * 0.1
            selected[neighbor.chunk_id] = neighbor
        ordered = sorted(
            selected.values(),
            key=lambda item: (
                _chunk_index(item) if _chunk_index(item) is not None else 10**6,
                item.chunk_id,
            ),
        )
        output.extend(ordered[: max(1, max_per_resource)])
        if len(output) >= max(1, limit):
            break
    output = output[: max(1, limit)]
    for rank, result in enumerate(output, start=1):
        result.rank = rank
    return output


def _resource_key(result: SearchResult) -> str:
    return str(
        result.source.get("resource_id")
        or result.source.get("knowledge_item_id")
        or result.chunk_id
    )


def _chunk_index(result: SearchResult) -> int | None:
    return _int_or_none(result.source.get("chunk_index"))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def rrf_fuse(
    branches: dict[str, list[SearchResult]],
    *,
    k: int,
    weights: dict[str, float],
) -> list[SearchResult]:
    scores: dict[str, float] = {}
    values: dict[str, SearchResult] = {}
    protected: set[str] = set()
    for name, results in branches.items():
        weight = float(weights.get(name, 1.0))
        for rank, result in enumerate(results, start=1):
            key = result.source.get("logical_position_key") or result.chunk_id
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            if key in values:
                _merge_provenance(values[key], result)
            if key not in values or (
                result.source.get("content_variant") == "protected"
                and key not in protected
            ):
                values[key] = result
                if result.source.get("content_variant") == "protected":
                    protected.add(key)
    ordered = sorted(values.items(), key=lambda item: (-scores[item[0]], item[0]))
    output: list[SearchResult] = []
    for rank, (key, result) in enumerate(ordered, start=1):
        result.score = scores[key]
        result.source["rrf_score"] = result.score
        result.source["retrieval_role"] = "anchor"
        result.rank = rank
        output.append(result)
    return output


def _merge_provenance(target: SearchResult, incoming: SearchResult) -> None:
    for key in (
        "bm25_score",
        "bm25_rank",
        "vector_score",
        "vector_rank",
    ):
        if key not in target.source and key in incoming.source:
            target.source[key] = incoming.source[key]
    matched = {
        str(value)
        for value in target.source.get("matched_by", ())
        if str(value)
    }
    matched.update(
        str(value)
        for value in incoming.source.get("matched_by", ())
        if str(value)
    )
    if matched:
        target.source["matched_by"] = sorted(matched)


def dedupe_logical_positions(results: list[SearchResult]) -> list[SearchResult]:
    selected: dict[str, SearchResult] = {}
    order: list[str] = []
    for result in results:
        key = str(result.source.get("logical_position_key") or result.chunk_id)
        current = selected.get(key)
        if current is None:
            selected[key] = result
            order.append(key)
            continue
        current_protected = current.source.get("content_variant") == "protected"
        result_protected = result.source.get("content_variant") == "protected"
        if result_protected and not current_protected:
            selected[key] = result
        elif result_protected == current_protected and result.score > current.score:
            selected[key] = result
    return [selected[key] for key in order]


def dedupe_by_resource(
    results: list[SearchResult],
    *,
    limit: int,
    max_per_resource: int | None = None,
) -> list[SearchResult]:
    counts: dict[str, int] = {}
    output: list[SearchResult] = []
    limit_per_resource = max(1, max_per_resource or settings.max_chunks_per_item)
    for result in results:
        key = str(result.source.get("resource_id") or result.source.get("knowledge_item_id") or result.chunk_id)
        if counts.get(key, 0) >= limit_per_resource:
            continue
        counts[key] = counts.get(key, 0) + 1
        result.rank = len(output) + 1
        output.append(result)
        if len(output) >= max(1, limit):
            break
    return output


def redact_query(value: str) -> str:
    text = re.sub(r"\b\d{6,}\b", "[redacted]", str(value or ""))
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted]", text)
    return text[:2000]


def _resolve_time_request(request: SearchRequest) -> SearchRequest:
    if request.occurred_after or request.occurred_before:
        return request
    text = request.query
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})?\s*月?\s*(\d{1,2})?\s*[日号]?", text)
    if not match:
        return request
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = match.group(3)
    start = datetime(year, month, int(day), tzinfo=ZoneInfo("Asia/Shanghai")) if day else datetime(year, month, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    if day:
        end = start + timedelta(days=1)
    elif match.group(2):
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        end = datetime(year + 1, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    return replace(
        request,
        occurred_after=start.astimezone(timezone.utc).isoformat(),
        occurred_before=end.astimezone(timezone.utc).isoformat(),
    )


def _citation(result: SearchResult, rank: int) -> dict[str, Any]:
    source = result.source
    return {
        "rank": rank,
        "chunk_id": result.chunk_id,
        "knowledge_item_id": source.get("knowledge_item_id"),
        "resource_type": source.get("resource_type"),
        "resource_id": source.get("resource_id"),
        "title": source.get("title"),
        "file_name": source.get("file_name"),
        "sent_at": source.get("sent_at"),
        "content_variant": source.get("content_variant"),
    }
