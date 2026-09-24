from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Iterable

from app.application.ports import ChunkIndexer
from app.config import settings
from app.domain.models import ChunkRecord, SearchResult


class ElasticsearchUnavailable(RuntimeError):
    pass


class ElasticsearchChunkStore(ChunkIndexer):
    """ES 9 adapter shared by indexing and retrieval.

    The constructor is lazy-friendly: the package is imported only when this
    adapter is actually bootstrapped, so local preprocessing tests do not need
    a running ES node.
    """

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or _build_client()
        self.display_index = settings.elasticsearch_display_index
        self.protected_index = settings.elasticsearch_protected_index

    def create_indices(self, *, overwrite: bool = False) -> list[str]:
        mapping = _load_mapping()
        created: list[str] = []
        for index in (self.display_index, self.protected_index):
            exists = bool(self.client.indices.exists(index=index))
            if exists and not overwrite:
                continue
            if exists and overwrite:
                self.client.indices.delete(index=index)
            self.client.indices.create(index=index, settings=mapping.get("settings", {}), mappings=mapping.get("mappings", {}))
            created.append(index)
        return created

    def index_chunks(self, chunks: list[ChunkRecord]) -> int:
        if not chunks:
            return 0
        operations: list[dict[str, Any]] = []
        for chunk in chunks:
            self._validate_chunk(chunk)
            index = self.protected_index if chunk.protected else self.display_index
            source = chunk.as_es_source()
            source["source_locator"] = _safe_source_locator(source.get("source_locator"))
            operations.extend(({"index": {"_index": index, "_id": chunk.chunk_id}}, source))
        response = self._bulk(operations)
        if isinstance(response, dict) and response.get("errors"):
            failed = [item for item in (response.get("items") or []) if _bulk_item_failed(item)]
            raise RuntimeError(f"Elasticsearch bulk indexing failed for {len(failed)} item(s)")
        return len(chunks)

    def delete_version(self, *, knowledge_item_id: str, content_version: int) -> int:
        total = 0
        query = {"bool": {"filter": [
            {"term": {"knowledge_item_id": knowledge_item_id}},
            {"term": {"content_version": content_version}},
        ]}}
        for index in (self.display_index, self.protected_index):
            try:
                result = self.client.delete_by_query(index=index, query=query, conflicts="proceed", wait_for_completion=True)
            except Exception as exc:
                if _is_not_found(exc):
                    continue
                raise
            total += int(result.get("deleted", 0)) if isinstance(result, dict) else 0
        return total

    def delete_older_versions(self, *, knowledge_item_id: str, content_version: int) -> int:
        total = 0
        query = {"bool": {"filter": [
            {"term": {"knowledge_item_id": knowledge_item_id}},
            {"range": {"content_version": {"lt": content_version}}},
        ]}}
        for index in (self.display_index, self.protected_index):
            try:
                result = self.client.delete_by_query(index=index, query=query, conflicts="proceed", wait_for_completion=True)
            except Exception as exc:
                if _is_not_found(exc):
                    continue
                raise
            total += int(result.get("deleted", 0)) if isinstance(result, dict) else 0
        return total

    def search_bm25(
        self,
        *,
        index: str,
        query_text: str,
        filters: list[dict[str, Any]],
        size: int,
        highlight: bool = False,
    ) -> list[SearchResult]:
        # operator "or", not "and".  With "and" every analysed term of the query
        # had to appear in one field, which no natural-language question can
        # satisfy: a 67-character sentence returned 0 documents, so the keyword
        # leg contributed nothing and RRF ran on the vector branch alone.  The
        # same sentence under "or" returns 226 documents with the gold chunk at
        # rank 1.  Short lookups are unaffected — a one or two term query scores
        # identically either way (measured on "2026-2027": 27 hits, same order).
        must = [{"multi_match": {"query": query_text, "fields": ["title^2", "content", "file_name^2", "sender_display_name"], "operator": "or"}}]
        query = {"bool": {"must": must, "filter": filters}}
        kwargs: dict[str, Any] = {
            "index": index,
            "query": query,
            "size": size,
            "track_total_hits": False,
            "_source": {"excludes": ["embedding"]},
        }
        if highlight:
            kwargs["highlight"] = {"fields": {"content": {}, "title": {}, "file_name": {}}, "number_of_fragments": 1}
        try:
            response = self._search_request(kwargs)
        except Exception as exc:
            if _is_not_found(exc):
                return []
            raise ElasticsearchUnavailable("Elasticsearch BM25 search failed") from exc
        return _results_from_response(response)

    def search_knn(
        self,
        *,
        index: str,
        query_vector: list[float],
        filters: list[dict[str, Any]],
        k: int,
        num_candidates: int,
    ) -> list[SearchResult]:
        knn = {
            "field": "embedding",
            "query_vector": query_vector,
            "k": k,
            "num_candidates": max(k, num_candidates),
            "filter": filters,
        }
        try:
            response = self._search_request({
                "index": index,
                "knn": knn,
                "size": k,
                "track_total_hits": False,
                "_source": {"excludes": ["embedding"]},
            })
        except Exception as exc:
            if _is_not_found(exc):
                return []
            raise ElasticsearchUnavailable("Elasticsearch kNN search failed") from exc
        return _results_from_response(response)

    def search_context_chunks(
        self,
        *,
        conversation_id: str,
        sent_at: str,
        knowledge_base_ids: tuple[str, ...] = (),
        organization_id: str | None = None,
        window_minutes: int = 10,
        size: int = 6,
        authorized_object_keys: tuple[str, ...] = (),
        include_protected: bool = False,
    ) -> list[SearchResult]:
        """Return bounded same-conversation chunks around a source timestamp.

        This is deliberately metadata-only retrieval.  Final authorization is
        still performed by the caller immediately before prompt assembly.
        """
        try:
            anchor = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return []
        filters: list[dict[str, Any]] = [
            {"term": {"lifecycle_status": "active"}},
            {"term": {"conversation_group_id": conversation_id}},
            {"range": {"sent_at": {"gte": (anchor - timedelta(minutes=max(1, window_minutes))).isoformat(), "lte": (anchor + timedelta(minutes=max(1, window_minutes))).isoformat()}}},
        ]
        if knowledge_base_ids:
            filters.append({"terms": {"knowledge_base_id": list(knowledge_base_ids)}})
        if organization_id:
            filters.append({"term": {"organization_id": organization_id}})
        branches: list[tuple[str, list[dict[str, Any]]]] = [(self.display_index, filters)]
        if include_protected and authorized_object_keys:
            branches.append((self.protected_index, filters + [{"terms": {"auth_object_key": list(authorized_object_keys)}}]))
        output: list[SearchResult] = []
        for index, branch_filters in branches:
            try:
                response = self._search_request({
                    "index": index,
                    "query": {"bool": {"filter": branch_filters}},
                    "size": max(1, min(100, size)),
                    "sort": [{"sent_at": {"order": "asc", "missing": "_last"}}, {"chunk_index": {"order": "asc"}}],
                    "track_total_hits": False,
                    "_source": {"excludes": ["embedding"]},
                })
            except Exception as exc:
                if _is_not_found(exc):
                    continue
                raise ElasticsearchUnavailable("Elasticsearch context search failed") from exc
            output.extend(_results_from_response(response))
        output.sort(key=lambda value: (str(value.source.get("sent_at") or ""), int(value.source.get("chunk_index") or 0)))
        return output[: max(1, min(100, size))]

    def parallel_search(
        self,
        *,
        query_text: str,
        query_vector: list[float] | None,
        display_filters: list[dict[str, Any]],
        protected_filters: list[dict[str, Any]] | None,
        bm25_size: int,
        knn_size: int,
        num_candidates: int,
        highlight: bool = False,
    ) -> dict[str, list[SearchResult]]:
        jobs: dict[str, Any] = {}
        jobs["display_bm25"] = (self.search_bm25, {"index": self.display_index, "query_text": query_text, "filters": display_filters, "size": bm25_size, "highlight": highlight})
        if query_vector is not None:
            jobs["display_knn"] = (self.search_knn, {"index": self.display_index, "query_vector": query_vector, "filters": display_filters + [{"term": {"rag_eligible": True}}], "k": knn_size, "num_candidates": num_candidates})
        if protected_filters is not None:
            jobs["protected_bm25"] = (self.search_bm25, {"index": self.protected_index, "query_text": query_text, "filters": protected_filters, "size": bm25_size, "highlight": highlight})
            if query_vector is not None:
                jobs["protected_knn"] = (self.search_knn, {"index": self.protected_index, "query_vector": query_vector, "filters": protected_filters + [{"term": {"rag_eligible": True}}], "k": knn_size, "num_candidates": num_candidates})
        if not jobs:
            return {}
        output: dict[str, list[SearchResult]] = {}
        executor = ThreadPoolExecutor(max_workers=len(jobs))
        try:
            future_map = {name: executor.submit(fn, **kwargs) for name, (fn, kwargs) in jobs.items()}
            deadline = time.monotonic() + max(0.05, settings.search_deadline_ms / 1000)
            for name, future in future_map.items():
                try:
                    output[name] = future.result(timeout=max(0.01, deadline - time.monotonic()))
                except FutureTimeoutError:
                    future.cancel()
                    output[name] = []
                except Exception:
                    # A missing/temporarily unhealthy branch must not block
                    # the other retrieval path; authorization fail-closed is
                    # handled before this method is called.
                    output[name] = []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return output

    def _bulk(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self.client.bulk(operations=operations, refresh="wait_for")
        except TypeError:
            # Compatibility with older test doubles and the 8.x client API.
            return self.client.bulk(operations=operations)

    def _search_request(self, kwargs: dict[str, Any]) -> Any:
        kwargs = {**kwargs, "timeout": f"{max(1, settings.es_query_timeout_ms)}ms"}
        try:
            return self.client.search(**kwargs)
        except TypeError:
            # Keep compatibility with lightweight test doubles and clients
            # that do not expose the optional timeout argument.
            kwargs.pop("timeout", None)
            return self.client.search(**kwargs)

    @staticmethod
    def _validate_chunk(chunk: ChunkRecord) -> None:
        if not chunk.content.strip():
            raise ValueError("cannot index an empty chunk")
        if chunk.rag_eligible and (not chunk.embedding or len(chunk.embedding) != settings.embedding_dims):
            raise ValueError("eligible chunk has no embedding with configured dimensions")
        if chunk.protected and chunk.auth_object_key not in {f"knowledge_original:{chunk.auth_resource_id}", f"attachment_content:{chunk.auth_resource_id}"}:
            raise ValueError("protected chunk has an invalid authorization object key")
        if chunk.protected and chunk.content_visibility != "protected":
            raise ValueError("protected chunk must have protected visibility")
        if not chunk.protected and chunk.content_visibility != "display":
            raise ValueError("display chunk must have display visibility")
        if not chunk.chunk_type.strip():
            raise ValueError("chunk type is required")


def _build_client() -> Any:
    try:
        from elasticsearch import Elasticsearch
    except ImportError as exc:
        raise ElasticsearchUnavailable("elasticsearch package is required") from exc
    kwargs: dict[str, Any] = {
        "request_timeout": settings.elasticsearch_request_timeout_seconds,
        "max_retries": settings.elasticsearch_max_retries,
        "retry_on_timeout": settings.elasticsearch_retry_on_timeout,
        "connections_per_node": max(1, settings.elasticsearch_max_connections),
    }
    if settings.elasticsearch_username or settings.elasticsearch_password:
        kwargs["basic_auth"] = (settings.elasticsearch_username, settings.elasticsearch_password)
    elif settings.elasticsearch_api_key:
        kwargs["api_key"] = settings.elasticsearch_api_key
    if settings.elasticsearch_ca_cert_path:
        kwargs["ca_certs"] = settings.elasticsearch_ca_cert_path
    if settings.elasticsearch_verify_certs:
        kwargs["verify_certs"] = True
    else:
        kwargs["verify_certs"] = False
    return Elasticsearch(settings.elasticsearch_url, **kwargs)


def _load_mapping() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "config" / "elasticsearch" / "documents-index.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ElasticsearchUnavailable("Elasticsearch mapping file could not be loaded") from exc
    # Keep the physical mapping coupled to the configured embedding contract.
    vector = value.setdefault("mappings", {}).setdefault("properties", {}).get("embedding")
    if isinstance(vector, dict):
        vector["dims"] = settings.embedding_dims
    return value


def _results_from_response(response: Any) -> list[SearchResult]:
    # Elasticsearch 8/9 clients return ObjectApiResponse rather than a plain
    # dict. It still exposes mapping-style `get`, so normalize by capability
    # instead of dropping every live response as an empty result set.
    if isinstance(response, dict):
        payload = response
    elif hasattr(response, "body") and isinstance(response.body, dict):
        payload = response.body
    elif hasattr(response, "get"):
        payload = response
    else:
        payload = {}
    hits = payload.get("hits", {}).get("hits", [])
    output: list[SearchResult] = []
    for rank, hit in enumerate(hits, start=1):
        if not isinstance(hit, dict):
            continue
        source = dict(hit.get("_source") or {})
        highlight = hit.get("highlight") or {}
        fragments = next(iter(highlight.values()), []) if isinstance(highlight, dict) else []
        output.append(SearchResult(
            chunk_id=str(hit.get("_id") or source.get("chunk_id") or ""),
            content=str(source.get("content") or ""),
            score=float(hit.get("_score") or 0.0),
            rank=rank,
            source=source,
            highlight=str(fragments[0]) if isinstance(fragments, list) and fragments else None,
        ))
    return output


def _bulk_item_failed(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    operation = next(iter(item.values()), {})
    return isinstance(operation, dict) and int(operation.get("status", 200)) >= 300


def _is_not_found(exc: Exception) -> bool:
    return "not_found" in str(exc).lower() or "index_not_found" in str(exc).lower()


_SOURCE_LOCATOR_FIELDS = {
    "page_number", "paragraph_index", "slide_number", "sheet_name",
    "row_start", "row_end", "column_start", "column_end", "char_start", "char_end",
}


def _safe_source_locator(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in _SOURCE_LOCATOR_FIELDS and item is not None}
