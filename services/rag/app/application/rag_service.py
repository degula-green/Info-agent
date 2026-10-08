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
from app.domain.rag import AccessCheck, AuthorizationScope, SearchRequest, SearchResult


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
        if scope.denied:
            raise AuthorizationDenied("authorization denied")
        if scope.failed or scope.truncated:
            raise AuthorizationUnavailable("authorization scope is incomplete")
        if not scope.available:
            request = replace(request, include_protected=False)
            protected_keys = ()
        entity_ids, entity_matches = self._resolve_entities(request)
        query_vector: list[float] | None = None
        degraded: list[str] = []
        if request.entry not in {"knowledge", "sources"}:
            try:
                vectors = self.embedding.embed([request.query])
                query_vector = vectors[0] if vectors else None
            except Exception:
                degraded.append("embedding_failed")
        global_branches: dict[str, list[SearchResult]] = {}
        try:
            global_branches["bm25"] = _annotate_branch(
                "bm25",
                self.indexer.search_bm25(
                    request,
                    protected_object_keys=protected_keys,
                ),
            )
            if query_vector is not None:
                global_branches["knn"] = _annotate_branch(
                    "knn",
                    self.indexer.search_knn(
                        request,
                        query_vector,
                        protected_object_keys=protected_keys,
                    ),
                )
        except Exception as exc:
            raise SearchUnavailable("Elasticsearch retrieval failed") from exc
        branch_branches: dict[str, list[SearchResult]] = {}
        if settings.tree_mode != "off" and entity_ids:
            try:
                branch_branches["branch_bm25"] = _annotate_branch(
                    "bm25",
                    self.indexer.search_bm25(
                        request,
                        entity_ids=entity_ids,
                        protected_object_keys=protected_keys,
                    ),
                )
                if query_vector is not None:
                    branch_branches["branch_knn"] = _annotate_branch(
                        "knn",
                        self.indexer.search_knn(
                            request,
                            query_vector,
                            entity_ids=entity_ids,
                            protected_object_keys=protected_keys,
                        ),
                    )
            except Exception:
                degraded.append("branch_failed")
                branch_branches = {}
        effective = dict(global_branches)
        if settings.tree_mode == "boost":
            effective.update(branch_branches)
        raw_global_candidate_count = sum(len(value) for value in global_branches.values())
        raw_branch_candidate_count = sum(len(value) for value in branch_branches.values())
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
            "tree_mode": settings.tree_mode,
            "authorization_snapshot_id": scope.snapshot_id,
            "authorized_protected_object_count": len(protected_keys),
            "scope_truncated": scope.truncated,
            "resolved_entity_count": len(entity_matches),
            "resolved_entity_count": len(entity_ids),
            "branch_candidate_count": raw_branch_candidate_count,
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
                "metadata_filter" if request.entry == "sources"
                else "tree_boost" if settings.tree_mode == "boost" and branch_branches
                else "tree_shadow" if settings.tree_mode == "shadow"
                else "traditional"
            ),
            "fallback_reason": (
                None if request.entry == "sources"
                else "no_entity_match" if settings.tree_mode != "off" and not entity_ids
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

    def _resolve_entities(self, request: SearchRequest) -> tuple[tuple[str, ...], list[Any]]:
        if settings.tree_mode == "off" or request.entry == "sources":
            return (), []
        entities, aliases, version = self.repository.load_entity_registry(
            scope_type=request.scope_type,
            scope_id=request.scope_id,
        )
        matcher = EntityMatcher(entities, aliases, registry_version=version)
        matches = matcher.match_text(request.query)
        # Time no longer narrows the entity set: occurred_after/occurred_before
        # are applied as ES metadata filters instead of a monthly branch key.
        values = tuple(
            dict.fromkeys(match.entity_id for match in matches)
        )
        return values[: settings.tree_max_entities], matches

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
