from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.application.ports import AuthorizationGateway, EmbeddingProvider
from app.application.retrieval.fusion import deduplicate_results, reciprocal_rank_fusion
from app.application.retrieval.query_planner import QueryPlan, plan_query
from app.application.retrieval.rerank import Reranker
from app.config import settings
from app.domain.models import AccessCheck, AuthorizationScope, SearchRequest, SearchResult


@dataclass(frozen=True)
class RetrievalDiagnostics:
    plan: QueryPlan
    protected_scope_available: bool
    protected_scope_count: int
    candidate_count: int
    authorized_count: int
    degraded: tuple[str, ...] = ()


class HybridRetriever:
    def __init__(self, *, store: Any, authorization: AuthorizationGateway, embedding: EmbeddingProvider, reranker: Reranker | None = None) -> None:
        self.store = store
        self.authorization = authorization
        self.embedding = embedding
        self.reranker = reranker

    def retrieve(self, request: SearchRequest) -> tuple[list[SearchResult], RetrievalDiagnostics]:
        plan = plan_query(request)
        if not plan.normalized_query:
            return [], RetrievalDiagnostics(plan, False, 0, 0, 0, ("empty_query",))
        if plan.time_resolved:
            request = SearchRequest(**{**request.__dict__, "occurred_after": plan.time_after, "occurred_before": plan.time_before})
        degraded: list[str] = []
        vector: list[float] | None = None
        display_filters = _display_filters(request)
        protected_scope: AuthorizationScope | None = None
        protected_filters: list[dict[str, Any]] | None = None
        with ThreadPoolExecutor(max_workers=2) as executor:
            vector_future = executor.submit(self.embedding.embed, [plan.normalized_query]) if plan.use_vector else None
            scope_future = executor.submit(
                self.authorization.search_scope,
                user_id=request.user_id,
                organization_id=request.organization_id,
                resource_parts=("original", "content"),
                knowledge_base_id=request.knowledge_base_id,
                knowledge_base_ids=tuple(request.knowledge_base_ids),
            ) if request.include_protected else None
            if vector_future is not None:
                try:
                    values = vector_future.result()
                    vector = values[0] if values else None
                except Exception:
                    degraded.append("embedding_failed")
            if scope_future is not None:
                try:
                    protected_scope = scope_future.result()
                except Exception:
                    protected_scope = AuthorizationScope(available=False)
            if protected_scope and protected_scope.available and protected_scope.object_keys:
                keys = [key for key in protected_scope.object_keys if key != "*"]
                if keys:
                    protected_filters = [{"terms": {"auth_object_key": keys}}]
                    protected_filters.extend(_common_filters(request))
            else:
                # Fail closed: an unavailable or empty scope must never become
                # an unfiltered protected search.
                protected_filters = None

        branches = self.store.parallel_search(
            query_text=plan.normalized_query,
            query_vector=vector,
            display_filters=display_filters,
            protected_filters=protected_filters,
            bm25_size=settings.bm25_top_k,
            knn_size=settings.knn_top_k,
            num_candidates=settings.knn_num_candidates,
            highlight=request.entry == "knowledge",
        )
        if protected_filters is None:
            branches = {name: values for name, values in branches.items() if not name.startswith("protected_")}
        candidate_count = sum(len(value) for value in branches.values())
        authorized = self._authorize_candidates(request, branches, protected_scope)
        authorized_count = sum(len(value) for value in authorized.values())
        fused = reciprocal_rank_fusion(
            authorized,
            k=settings.rrf_k,
            weights={"display_bm25": settings.rrf_keyword_weight, "protected_bm25": settings.rrf_keyword_weight, "display_knn": settings.rrf_vector_weight, "protected_knn": settings.rrf_vector_weight},
        )
        if settings.rerank_enabled and self.reranker and fused:
            try:
                fused = self.reranker.rerank(plan.normalized_query, fused[: settings.rerank_top_n]) + fused[settings.rerank_top_n :]
            except Exception:
                degraded.append("rerank_failed")
        if request.entry == "ai":
            fused = [result for result in fused if bool(result.source.get("rag_eligible", True))]
        final = deduplicate_results(fused, max_per_item=settings.max_chunks_per_item, limit=max(1, request.top_k))
        return final, RetrievalDiagnostics(
            plan,
            bool(protected_scope and protected_scope.available),
            len(protected_scope.object_keys) if protected_scope else 0,
            candidate_count,
            authorized_count,
            tuple(degraded),
        )

    def authorize_results(
        self,
        request: SearchRequest,
        results: list[SearchResult],
        *,
        snapshot_id: str | None = None,
    ) -> list[SearchResult]:
        """Perform the final just-before-generation permission check."""
        if not results:
            return []
        checks = [
            AccessCheck(item.auth_resource_type, item.auth_resource_part, item.auth_resource_id, "view")
            for item in results
        ]
        decisions = self.authorization.check_batch(
            user_id=request.user_id,
            organization_id=request.organization_id,
            checks=checks,
            snapshot_id=snapshot_id,
        )
        if len(decisions) != len(results):
            return []
        return [item for item, allowed in zip(results, decisions) if bool(allowed)]

    def _authorize_candidates(self, request: SearchRequest, branches: dict[str, list[SearchResult]], scope: AuthorizationScope | None) -> dict[str, list[SearchResult]]:
        all_results: list[SearchResult] = []
        locations: list[tuple[str, SearchResult]] = []
        for name, values in branches.items():
            for value in values:
                all_results.append(value)
                locations.append((name, value))
        checks = [AccessCheck(item.auth_resource_type, item.auth_resource_part, item.auth_resource_id, "view") for item in all_results]
        unique_checks: list[AccessCheck] = []
        seen: set[tuple[str, str, str, str]] = set()
        for check in checks:
            key = (check.resource_type, check.resource_part, check.resource_id, check.action)
            if key not in seen:
                seen.add(key)
                unique_checks.append(check)
        decisions = self.authorization.check_batch(
            user_id=request.user_id,
            organization_id=request.organization_id,
            checks=unique_checks,
            snapshot_id=scope.snapshot_id if scope else None,
        ) if unique_checks else []
        decision_map = {(check.resource_type, check.resource_part, check.resource_id, check.action): bool(value) for check, value in zip(unique_checks, decisions)}
        output: dict[str, list[SearchResult]] = {name: [] for name in branches}
        for name, result in locations:
            key = (result.auth_resource_type, result.auth_resource_part, result.auth_resource_id, "view")
            if decision_map.get(key, False):
                output[name].append(result)
        return output


def _common_filters(request: SearchRequest) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [{"term": {"lifecycle_status": "active"}}]
    knowledge_base_ids = tuple(request.knowledge_base_ids) or ((request.knowledge_base_id,) if request.knowledge_base_id else ())
    # Explicit library ids are the ES scope. Do not also require organization_id,
    # or private bases (owner-only, empty organization_id) are silently dropped
    # when the caller passes both an org context and mixed kb ids — the same
    # combination AI Q&A uses.
    if knowledge_base_ids:
        filters.append({"terms": {"knowledge_base_id": list(knowledge_base_ids)}})
    elif request.organization_id:
        filters.append({"term": {"organization_id": request.organization_id}})
    if request.resource_types:
        filters.append({"bool": {"should": [
            {"terms": {"part_kind": list(request.resource_types)}},
            {"terms": {"auth_resource_type": list(request.resource_types)}},
        ], "minimum_should_match": 1}})
    if request.conversation_id:
        filters.append({"term": {"conversation_group_id": request.conversation_id}})
    source_should: list[dict[str, Any]] = []
    if request.source_attachment_ids:
        source_should.append({"terms": {"attachment_id": list(request.source_attachment_ids)}})
    if request.source_knowledge_item_ids:
        source_should.append({"terms": {"knowledge_item_id": list(request.source_knowledge_item_ids)}})
    if source_should:
        filters.append({"bool": {"should": source_should, "minimum_should_match": 1}})
    if request.source_chunk_ids:
        # Branch-T scope: the Chunk ids the tree navigated to. This is a plain
        # AND filter, deliberately not folded into source_should above, so a
        # tree scope can never be widened by a sibling attachment/item clause.
        filters.append({"terms": {"chunk_id": list(request.source_chunk_ids)}})
    return filters


def _display_filters(request: SearchRequest) -> list[dict[str, Any]]:
    filters = _common_filters(request)
    if request.organization_id and not request.user_id:
        return filters
    # The final Service 1 check remains authoritative; these terms only reduce
    # the candidate set and never grant access.
    if request.user_id and not (request.knowledge_base_id or request.knowledge_base_ids):
        should = [{"term": {"owner_user_id": request.user_id}}]
        if request.organization_id:
            should.append({"term": {"organization_id": request.organization_id}})
        filters.append({"bool": {"should": should, "minimum_should_match": 1}})
    if request.sender_name:
        filters.append({"match": {"sender_display_name": request.sender_name}})
    if request.occurred_after:
        filters.append({"range": {"sent_at": {"gte": request.occurred_after}}})
    if request.occurred_before:
        filters.append({"range": {"sent_at": {"lte": request.occurred_before}}})
    return filters
