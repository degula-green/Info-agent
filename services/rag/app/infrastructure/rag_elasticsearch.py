from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.domain.rag import Chunk, SearchRequest, SearchResult


class ElasticsearchUnavailable(RuntimeError):
    pass


class RagChunkIndex:
    def __init__(self, client: Any | None = None) -> None:
        self.client = client or build_client()

    def create_indices(self, *, recreate: bool = False) -> list[str]:
        mapping = load_mapping()
        created: list[str] = []
        for physical, read_alias, write_alias in (
            ("rag_chunks_display_v1", settings.elasticsearch_display_read_index, settings.elasticsearch_display_write_index),
            ("rag_chunks_protected_v1", settings.elasticsearch_protected_read_index, settings.elasticsearch_protected_write_index),
        ):
            exists = bool(self.client.indices.exists(index=physical))
            if exists and recreate:
                self.client.indices.delete(index=physical)
                exists = False
            if not exists:
                self.client.indices.create(
                    index=physical,
                    settings=mapping.get("settings", {}),
                    mappings=mapping.get("mappings", {}),
                )
                created.append(physical)
            else:
                self.client.indices.put_mapping(
                    index=physical,
                    properties=mapping["mappings"]["properties"],
                )
            actions = []
            for alias, is_write in ((read_alias, False), (write_alias, True)):
                if not bool(self.client.indices.exists_alias(name=alias)):
                    action = {"add": {"index": physical, "alias": alias}}
                    if is_write:
                        action["add"]["is_write_index"] = True
                    actions.append(action)
            if actions:
                self.client.indices.update_aliases(actions=actions)
        return created

    def index_chunks(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        operations: list[dict[str, Any]] = []
        for chunk in chunks:
            if chunk.rag_eligible and (
                not chunk.embedding or len(chunk.embedding) != settings.embedding_dims
            ):
                raise ValueError(f"eligible chunk {chunk.chunk_id} has no valid embedding")
            alias = (
                settings.elasticsearch_protected_write_index
                if chunk.protected
                else settings.elasticsearch_display_write_index
            )
            operations.extend((
                {"index": {"_index": alias, "_id": chunk.chunk_id}},
                chunk.es_source(),
            ))
        response = self.client.bulk(operations=operations, refresh="wait_for")
        if isinstance(response, dict) and response.get("errors"):
            failures = [
                item for item in response.get("items", [])
                if _bulk_failed(item)
            ]
            raise ElasticsearchUnavailable(f"bulk indexing failed for {len(failures)} chunk(s)")
        return len(chunks)

    def delete_older_versions(self, *, resource_id: str, content_version: int) -> int:
        total = 0
        for alias in (
            settings.elasticsearch_display_write_index,
            settings.elasticsearch_protected_write_index,
        ):
            try:
                response = self.client.delete_by_query(
                    index=alias,
                    conflicts="proceed",
                    query={"bool": {"filter": [
                        {"term": {"resource_id": resource_id}},
                        {"range": {"content_version": {"lt": content_version}}},
                    ]}},
                )
            except Exception as exc:
                if _not_found(exc):
                    continue
                raise
            if isinstance(response, dict):
                total += int(response.get("deleted") or 0)
        return total

    def search_bm25(
        self,
        request: SearchRequest,
        *,
        branch_keys: tuple[str, ...] = (),
        protected_object_keys: tuple[str, ...] = (),
        size: int | None = None,
    ) -> list[SearchResult]:
        branches = [
            (settings.elasticsearch_display_read_index, _filters(request, branch_keys=branch_keys)),
        ]
        if request.include_protected and protected_object_keys:
            branches.append((
                settings.elasticsearch_protected_read_index,
                _filters(request, branch_keys=branch_keys, protected_object_keys=protected_object_keys),
            ))
        output: list[SearchResult] = []
        for index, filters in branches:
            response = self._search(
                index=index,
                query=_bm25_query(request.query, filters),
                size=size or settings.bm25_top_k,
            )
            output.extend(_results(response))
        return _dedupe_display_protected(output)

    def search_knn(
        self,
        request: SearchRequest,
        query_vector: list[float],
        *,
        branch_keys: tuple[str, ...] = (),
        protected_object_keys: tuple[str, ...] = (),
        size: int | None = None,
    ) -> list[SearchResult]:
        branches = [
            (settings.elasticsearch_display_read_index, _filters(request, branch_keys=branch_keys)),
        ]
        if request.include_protected and protected_object_keys:
            branches.append((
                settings.elasticsearch_protected_read_index,
                _filters(request, branch_keys=branch_keys, protected_object_keys=protected_object_keys),
            ))
        output: list[SearchResult] = []
        for index, filters in branches:
            response = self._search(
                index=index,
                knn={
                    "field": "embedding",
                    "query_vector": query_vector,
                    "k": size or settings.knn_top_k,
                    "num_candidates": settings.knn_num_candidates,
                    "filter": filters,
                },
                size=size or settings.knn_top_k,
            )
            output.extend(_results(response))
        return _dedupe_display_protected(output)

    def _search(self, **kwargs: Any) -> Any:
        try:
            return self.client.search(
                **kwargs,
                timeout=f"{max(1, settings.es_query_timeout_ms)}ms",
                track_total_hits=False,
                _source={"excludes": ["embedding"]},
            )
        except TypeError:
            return self.client.search(**kwargs)
        except Exception as exc:
            if _not_found(exc):
                return {"hits": {"hits": []}}
            raise ElasticsearchUnavailable("Elasticsearch search failed") from exc


def _bm25_query(query: str, filters: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "bool": {
            "must": [{
                "multi_match": {
                    "query": query,
                    "fields": ["title^3", "file_name^2", "heading_path", "content"],
                    "operator": "or",
                }
            }],
            "filter": filters,
        }
    }


def _filters(
    request: SearchRequest,
    *,
    branch_keys: tuple[str, ...] = (),
    protected_object_keys: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = [
        {"term": {"scope_key": request.scope_key}},
        {"term": {"lifecycle_status": "active"}},
        {"term": {"rag_eligible": True}},
    ]
    if request.knowledge_base_ids:
        output.append({"terms": {"knowledge_base_id": list(request.knowledge_base_ids)}})
    if request.occurred_after:
        output.append({"range": {"sent_at": {"gte": request.occurred_after}}})
    if request.occurred_before:
        output.append({"range": {"sent_at": {"lte": request.occurred_before}}})
    if request.conversation_id:
        output.append({"term": {"source_conversation_id": request.conversation_id}})
    if branch_keys:
        output.append({"terms": {"branch_keys": list(branch_keys)}})
    if protected_object_keys:
        output.append({"terms": {"auth_object_key": list(protected_object_keys)}})
    return output


def _results(response: Any) -> list[SearchResult]:
    if isinstance(response, dict):
        payload = response
    elif hasattr(response, "body") and isinstance(response.body, dict):
        payload = response.body
    elif hasattr(response, "get"):
        payload = response
    else:
        payload = {}
    output: list[SearchResult] = []
    for index, hit in enumerate(payload.get("hits", {}).get("hits", []), start=1):
        if not isinstance(hit, dict):
            continue
        source = dict(hit.get("_source") or {})
        output.append(SearchResult(
            chunk_id=str(hit.get("_id") or source.get("chunk_id") or ""),
            content=str(source.get("content") or ""),
            score=float(hit.get("_score") or 0.0),
            rank=index,
            source=source,
        ))
    return output


def _dedupe_display_protected(results: list[SearchResult]) -> list[SearchResult]:
    selected: dict[str, SearchResult] = {}
    for result in results:
        key = str(result.source.get("logical_position_key") or result.chunk_id)
        current = selected.get(key)
        if current is None:
            selected[key] = result
            continue
        current_protected = current.source.get("content_variant") == "protected"
        result_protected = result.source.get("content_variant") == "protected"
        if result_protected and not current_protected:
            selected[key] = result
        elif result_protected == current_protected and result.score > current.score:
            selected[key] = result
    return sorted(selected.values(), key=lambda item: item.score, reverse=True)


def _bulk_failed(item: Any) -> bool:
    operation = next(iter(item.values()), {}) if isinstance(item, dict) else {}
    return int(operation.get("status", 200)) >= 300


def _not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not_found" in text or "index_not_found" in text


def build_client() -> Any:
    try:
        from elasticsearch import Elasticsearch
    except ImportError as exc:
        raise ElasticsearchUnavailable("elasticsearch package is required") from exc
    kwargs: dict[str, Any] = {
        "request_timeout": settings.elasticsearch_request_timeout_seconds,
        "max_retries": settings.elasticsearch_max_retries,
        "retry_on_timeout": settings.elasticsearch_retry_on_timeout,
        "connections_per_node": max(1, settings.elasticsearch_max_connections),
        "verify_certs": settings.elasticsearch_verify_certs,
    }
    if settings.elasticsearch_username or settings.elasticsearch_password:
        kwargs["basic_auth"] = (
            settings.elasticsearch_username,
            settings.elasticsearch_password,
        )
    elif settings.elasticsearch_api_key:
        kwargs["api_key"] = settings.elasticsearch_api_key
    if settings.elasticsearch_ca_cert_path:
        kwargs["ca_certs"] = settings.elasticsearch_ca_cert_path
    return Elasticsearch(settings.elasticsearch_url, **kwargs)


def load_mapping() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "config" / "elasticsearch" / "rag-chunks-index-v1.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ElasticsearchUnavailable("RAG ES mapping could not be loaded") from exc
    value["mappings"]["properties"]["embedding"]["dims"] = settings.embedding_dims
    return value
