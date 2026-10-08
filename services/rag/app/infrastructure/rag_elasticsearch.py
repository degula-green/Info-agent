from __future__ import annotations

import json
import re
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
        payload = _response_body(response)
        if payload.get("errors"):
            failures = [
                item for item in payload.get("items", [])
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

    def update_chunk_branches(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        operations: list[dict[str, Any]] = []
        for chunk in chunks:
            alias = (
                settings.elasticsearch_protected_write_index
                if chunk.protected
                else settings.elasticsearch_display_write_index
            )
            operations.extend((
                {"update": {"_index": alias, "_id": chunk.chunk_id}},
                {"doc": {
                    "branch_keys": list(chunk.branch_keys),
                    "registry_version": chunk.registry_version,
                }},
            ))
        response = self.client.bulk(operations=operations, refresh="wait_for")
        if _response_body(response).get("errors"):
            raise ElasticsearchUnavailable("branch projection update failed")
        return len(chunks)

    def search_bm25(
        self,
        request: SearchRequest,
        *,
        branch_keys: tuple[str, ...] = (),
        protected_object_keys: tuple[str, ...] = (),
        size: int | None = None,
    ) -> list[SearchResult]:
        page_size = size or (
            request.top_k
            if request.entry in {"sources", "export"}
            else settings.bm25_top_k
        )
        branches = [
            (settings.elasticsearch_display_read_index, _filters(request, branch_keys=branch_keys)),
        ]
        if request.include_protected:
            # Do NOT filter the protected index by the enumerated key list.
            # OpenFGA's list-objects can return a capped, unstable subset, so a
            # genuinely authorized original may be missing from it and its
            # masked display twin will not match the query. Retrieval returns
            # candidates and RAG authorizes each one exactly before exposing it.
            branches.append((
                settings.elasticsearch_protected_read_index,
                _filters(request, branch_keys=branch_keys),
            ))
        output: list[SearchResult] = []
        for index, filters in branches:
            search_kwargs: dict[str, Any] = {
                "index": index,
                "query": _bm25_query(request.query, filters),
                "size": page_size,
                "sort": (
                    [
                        {"sent_at": {"order": "desc", "missing": "_last"}},
                        {"chunk_id": {"order": "asc"}},
                    ]
                    if not request.query.strip()
                    else None
                ),
            }
            if request.offset:
                search_kwargs["from_"] = request.offset
            response = self._search(**search_kwargs)
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
        if request.include_protected:
            branches.append((
                settings.elasticsearch_protected_read_index,
                _filters(request, branch_keys=branch_keys),
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

    def search_neighbors(
        self,
        request: SearchRequest,
        anchors: list[SearchResult],
        *,
        protected_object_keys: tuple[str, ...] = (),
        radius: int = 1,
    ) -> list[SearchResult]:
        if radius < 1:
            return []
        output: list[SearchResult] = []
        seen: set[str] = set()
        for anchor in anchors:
            source = anchor.source
            if source.get("resource_type") != "attachment":
                continue
            chunk_index = _int_or_none(source.get("chunk_index"))
            knowledge_item_id = _text_or_none(source.get("knowledge_item_id"))
            content_version = _int_or_none(source.get("content_version"))
            content_variant = _text_or_none(source.get("content_variant")) or "display"
            if chunk_index is None or knowledge_item_id is None or content_version is None:
                continue
            neighbor_indexes = list(range(max(0, chunk_index - radius), chunk_index + radius + 1))
            neighbor_indexes = [value for value in neighbor_indexes if value != chunk_index]
            if not neighbor_indexes:
                continue
            protected = content_variant == "protected"
            if protected and not request.include_protected:
                continue
            index = (
                settings.elasticsearch_protected_read_index
                if protected
                else settings.elasticsearch_display_read_index
            )
            # The anchor already passed an exact authorization check. Its own
            # knowledge_item_id is the safe filter; the enumerated key list is
            # not used because it can be incomplete.
            filters = _filters(request)
            filters.extend([
                {"term": {"knowledge_item_id": knowledge_item_id}},
                {"term": {"content_version": content_version}},
                {"term": {"content_variant": content_variant}},
                {"terms": {"chunk_index": neighbor_indexes}},
            ])
            query_key = f"{index}:{knowledge_item_id}:{content_version}:{content_variant}:{chunk_index}"
            if query_key in seen:
                continue
            seen.add(query_key)
            response = self._search(
                index=index,
                query={"bool": {"filter": filters}},
                size=len(neighbor_indexes),
            )
            output.extend(_results(response))
        unique: dict[str, SearchResult] = {}
        for result in output:
            unique[result.chunk_id] = result
        return list(unique.values())

    def search_message_context(
        self,
        request: SearchRequest,
        anchors: list[dict[str, Any]],
        *,
        radius: int = 2,
    ) -> list[SearchResult]:
        """Messages either side of an anchor, inside the anchor's conversation.

        A form value is often a bare line ("小呆呆") whose field word sits in a
        neighbouring message. The window is pinned to the anchor's own
        conversation by ordering on ``sent_at``; authorization still decides
        which of those neighbours may be shown.
        """

        if radius < 1:
            return []
        output: list[SearchResult] = []
        seen: set[str] = set()
        for anchor in anchors:
            if not isinstance(anchor, dict):
                continue
            conversation_id = str(anchor.get("conversation_id") or "").strip()
            sent_at = str(anchor.get("sent_at") or "").strip()
            resource_id = str(anchor.get("resource_id") or "").strip()
            if not conversation_id or not sent_at:
                continue
            key = f"{conversation_id}:{resource_id}"
            if key in seen:
                continue
            seen.add(key)
            base_filters = _filters(request)
            base_filters.extend(
                [
                    {"term": {"source_conversation_id": conversation_id}},
                    {"term": {"resource_type": "message"}},
                ]
            )
            if resource_id:
                base_filters.append(
                    {"bool": {"must_not": {"term": {"resource_id": resource_id}}}}
                )
            for direction, order in (("lt", "desc"), ("gt", "asc")):
                filters = [
                    *base_filters,
                    {"range": {"sent_at": {direction: sent_at}}},
                ]
                response = self._search(
                    index=settings.elasticsearch_display_read_index,
                    query={"bool": {"filter": filters}},
                    size=radius,
                    sort=[
                        {"sent_at": {"order": order}},
                        {"chunk_id": {"order": order}},
                    ],
                )
                output.extend(_results(response))
        unique: dict[str, SearchResult] = {}
        for result in output:
            unique[result.chunk_id] = result
        return list(unique.values())

    def search_protected_variants(
        self,
        request: SearchRequest,
        knowledge_item_ids: tuple[str, ...],
    ) -> list[SearchResult]:
        """Load the protected chunks for specific knowledge items.

        Authorization is checked per item by the caller; this method only
        hydrates the already-approved candidates. The scope filters still apply
        so a requested item from another tenant cannot be read by accident.
        """

        ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in knowledge_item_ids
                if str(value or "").strip()
            )
        )
        if not ids:
            return []
        response = self._search(
            index=settings.elasticsearch_protected_read_index,
            query={
                "bool": {
                    "filter": [
                        *_filters(request),
                        {"terms": {"knowledge_item_id": list(ids)}},
                        {"term": {"content_variant": "protected"}},
                    ]
                }
            },
            size=max(len(ids) * max(1, settings.max_chunks_per_item), len(ids)),
        )
        return _dedupe_display_protected(_results(response))

    def _search(self, *, sort: list[dict[str, Any]] | None = None, **kwargs: Any) -> Any:
        if sort:
            kwargs["sort"] = sort
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
    if not str(query or "").strip():
        return {"bool": {"filter": filters}}
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
    conversation_ids = tuple(
        dict.fromkeys(
            [
                value
                for value in (
                    request.source_conversation_id,
                    *request.conversation_ids,
                )
                if value
            ]
        )
    )
    conversation_should: list[dict[str, Any]] = []
    if conversation_ids:
        conversation_should.append(
            {"terms": {"source_conversation_id": list(conversation_ids)}}
        )
    if request.conversation_names:
        conversation_should.extend(
            {
                "match": {
                    "source_conversation_name": {
                        "query": name,
                        "operator": "and",
                    }
                }
            }
            for name in request.conversation_names
        )
    if (
        len(conversation_ids) == 1
        and request.source_conversation_id
        and not request.conversation_ids
        and not request.conversation_names
    ):
        output.append({"term": {"source_conversation_id": conversation_ids[0]}})
    elif conversation_should:
        output.append(
            {
                "bool": {
                    "should": conversation_should,
                    "minimum_should_match": 1,
                }
            }
        )
    sender_should: list[dict[str, Any]] = []
    if request.sender_ids:
        sender_should.append(
            {"terms": {"sender_identity_id": list(request.sender_ids)}}
        )
    if request.sender_names:
        sender_should.extend(
            {
                "match": {
                    "sender_display_name": {
                        "query": name,
                        "operator": "and",
                    }
                }
            }
            for name in request.sender_names
        )
    if sender_should:
        output.append(
            {
                "bool": {
                    "should": sender_should,
                    "minimum_should_match": 1,
                }
            }
        )
    if request.resource_ids:
        output.append({"terms": {"resource_id": list(request.resource_ids)}})
    if request.content_contains:
        output.extend(
            {"match_phrase": {"content": value}}
            for value in request.content_contains
            if str(value or "").strip()
        )
    if request.resource_types:
        output.append({"terms": {"resource_type": list(request.resource_types)}})
    if request.file_extensions:
        output.append(
            {
                "terms": {
                    "file_extension": [
                        str(value).lower().lstrip(".") for value in request.file_extensions
                    ]
                }
            }
        )
    if request.message_types:
        output.append({"terms": {"message_type": list(request.message_types)}})
    if branch_keys:
        should: list[dict[str, Any]] = []
        exact: list[str] = []
        for key in branch_keys:
            if re.search(r":\d{4}-\d{2}$", key):
                exact.append(key)
            else:
                should.append({"prefix": {"branch_keys": key}})
        if exact:
            should.append({"terms": {"branch_keys": exact}})
        output.append({"bool": {"should": should, "minimum_should_match": 1}})
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


def _response_body(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    body = getattr(response, "body", None)
    return body if isinstance(body, dict) else {}


def _not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not_found" in text or "index_not_found" in text


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


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
