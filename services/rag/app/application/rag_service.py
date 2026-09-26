from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.application.entity_service import EntityMatcher
from app.application.mvp_ports import (
    AuthorizationGateway,
    EmbeddingProvider,
    SearchIndexer,
)
from app.config import settings
from app.domain.rag import AccessCheck, AuthorizationScope, SearchRequest, SearchResult, time_bucket


class SearchUnavailable(RuntimeError):
    pass


class AuthorizationUnavailable(RuntimeError):
    pass


@dataclass
class RetrievalResponse:
    request_id: str
    results: list[SearchResult]
    diagnostics: dict[str, Any]


class RAGRetrievalService:
    def __init__(
        self,
        *,
        repository: object,
        indexer: SearchIndexer,
        embedding: EmbeddingProvider,
        authorization: AuthorizationGateway,
        answer_provider: Any | None = None,
    ) -> None:
        self.repository = repository
        self.indexer = indexer
        self.embedding = embedding
        self.authorization = authorization
        self.answer_provider = answer_provider

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
        if not scope.available or scope.truncated:
            request = replace(request, include_protected=False)
            protected_keys = ()
        branch_keys, entity_matches = self._resolve_branches(request)
        query_vector: list[float] | None = None
        degraded: list[str] = []
        if request.entry != "knowledge":
            try:
                vectors = self.embedding.embed([request.query])
                query_vector = vectors[0] if vectors else None
            except Exception:
                degraded.append("embedding_failed")
        global_branches: dict[str, list[SearchResult]] = {}
        try:
            global_branches["bm25"] = self.indexer.search_bm25(
                request,
                protected_object_keys=protected_keys,
            )
            if query_vector is not None:
                global_branches["knn"] = self.indexer.search_knn(
                    request,
                    query_vector,
                    protected_object_keys=protected_keys,
                )
        except Exception as exc:
            raise SearchUnavailable("Elasticsearch retrieval failed") from exc
        branch_branches: dict[str, list[SearchResult]] = {}
        if settings.tree_mode != "off" and branch_keys:
            try:
                branch_branches["branch_bm25"] = self.indexer.search_bm25(
                    request,
                    branch_keys=branch_keys,
                    protected_object_keys=protected_keys,
                )
                if query_vector is not None:
                    branch_branches["branch_knn"] = self.indexer.search_knn(
                        request,
                        query_vector,
                        branch_keys=branch_keys,
                        protected_object_keys=protected_keys,
                    )
            except Exception:
                degraded.append("branch_failed")
                branch_branches = {}
        effective = dict(global_branches)
        if settings.tree_mode == "boost":
            effective.update(branch_branches)
        effective = self._authorize_branches(request, effective, scope)
        fused = rrf_fuse(
            effective,
            k=settings.rrf_k,
            weights={
                "bm25": settings.rrf_keyword_weight,
                "knn": settings.rrf_vector_weight,
                "branch_bm25": settings.tree_branch_weight,
                "branch_knn": settings.tree_branch_weight,
            },
        )
        fused = dedupe_logical_positions(fused)
        results = dedupe_by_resource(fused, limit=request.top_k)
        # Final authorization is repeated immediately before the result leaves
        # the service; no candidate can enter a prompt on a stale decision.
        results = self._authorize_results(request, results, scope)
        diagnostics = {
            "tree_mode": settings.tree_mode,
            "authorization_snapshot_id": scope.snapshot_id,
            "authorized_protected_object_count": len(protected_keys),
            "scope_truncated": scope.truncated,
            "resolved_entity_count": len(entity_matches),
            "resolved_branch_count": len(branch_keys),
            "branch_candidate_count": sum(len(value) for value in branch_branches.values()),
            "global_candidate_count": sum(len(value) for value in global_branches.values()),
            "fused_result_count": len(results),
            "effective_execution_path": (
                "tree_boost" if settings.tree_mode == "boost" and branch_branches
                else "tree_shadow" if settings.tree_mode == "shadow"
                else "traditional"
            ),
            "fallback_reason": (
                "no_entity_match" if settings.tree_mode != "off" and not branch_keys
                else "branch_failed" if "branch_failed" in degraded
                else None
            ),
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
                tree_mode=settings.tree_mode,
                execution_path=diagnostics["effective_execution_path"],
                diagnostics=diagnostics,
                result_count=len(results),
                duration_ms=int((time.perf_counter() - started) * 1000),
                request_id=request_id,
            )
        except Exception:
            pass
        return RetrievalResponse(request_id=request_id, results=results, diagnostics=diagnostics)

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

    def _resolve_branches(self, request: SearchRequest) -> tuple[tuple[str, ...], list[Any]]:
        if settings.tree_mode == "off":
            return (), []
        entities, aliases, version = self.repository.load_entity_registry(
            scope_type=request.scope_type,
            scope_id=request.scope_id,
        )
        matcher = EntityMatcher(entities, aliases, registry_version=version)
        matches = matcher.match_text(request.query)
        bucket = time_bucket(request.occurred_after) or time_bucket(request.occurred_before)
        values = []
        for match in matches:
            base = f"entity:{match.domain}:{match.entity_id}"
            values.append(f"{base}:{bucket}" if bucket else base)
        return tuple(values[: settings.tree_max_branches]), matches

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
        result.rank = rank
        output.append(result)
    return output


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


def dedupe_by_resource(results: list[SearchResult], *, limit: int) -> list[SearchResult]:
    counts: dict[str, int] = {}
    output: list[SearchResult] = []
    for result in results:
        key = str(result.source.get("resource_id") or result.source.get("knowledge_item_id") or result.chunk_id)
        if counts.get(key, 0) >= settings.max_chunks_per_item:
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
