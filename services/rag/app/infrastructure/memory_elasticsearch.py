from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.domain.memory import MemoryGraph, TreeSearchRequest
from app.infrastructure.elasticsearch import ElasticsearchUnavailable, _build_client, _bulk_item_failed, _is_not_found


class ElasticsearchMemoryStore:
    def __init__(self, client: Any | None = None) -> None:
        self.client = client or _build_client()
        self.indices = {
            ("fact", "display"): settings.memory_display_facts_index,
            ("fact", "protected"): settings.memory_protected_facts_index,
            ("node", "display"): settings.memory_display_nodes_index,
            ("node", "protected"): settings.memory_protected_nodes_index,
        }
        self.write_indices = {
            key: self._write_alias(value) for key, value in self.indices.items()
        }

    def create_indices(self) -> list[str]:
        created = []
        for (kind, visibility), read_alias in self.indices.items():
            physical = f"memory_{visibility}_{kind}s_v1"
            write_alias = read_alias[:-5] + "_write" if read_alias.endswith("_read") else read_alias + "_write"
            if not bool(self.client.indices.exists(index=physical)):
                mapping = self._mapping(kind)
                self.client.indices.create(index=physical, settings=mapping["settings"], mappings=mapping["mappings"])
                created.append(physical)
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

    def index_graph(self, graph: MemoryGraph, vectors: dict[str, list[float]]) -> int:
        operations: list[dict[str, Any]] = []
        documents = [("node", row, "node_id") for row in graph.nodes] + [("fact", row, "fact_projection_id") for row in graph.fact_projections]
        desired_by_fact: dict[tuple[str, str], list[str]] = {}
        for row in graph.fact_projections:
            visibility = "protected" if row.get("visibility") == "protected" else "display"
            desired_by_fact.setdefault((visibility, str(row["fact_id"])), []).append(str(row["fact_projection_id"]))
        delete_by_query = getattr(self.client, "delete_by_query", None)
        if callable(delete_by_query):
            for (visibility, fact_id), desired_ids in desired_by_fact.items():
                delete_by_query(
                    index=self.indices[("fact", visibility)], conflicts="proceed", refresh=True,
                    query={"bool": {"filter": [{"term": {"fact_id": fact_id}}], "must_not": [{"ids": {"values": desired_ids}}]}},
                )
        for kind, source, id_field in documents:
            value = dict(source)
            document_id = str(value[id_field])
            vector = vectors.get(document_id)
            if vector is not None:
                value["embedding"] = vector
            visibility = "protected" if value.get("visibility") == "protected" else "display"
            operations.extend(({"index": {"_index": self.write_indices[(kind, visibility)], "_id": document_id}}, value))
        if not operations:
            return 0
        response = self.client.bulk(operations=operations, refresh="wait_for")
        if isinstance(response, dict) and response.get("errors"):
            failed = [item for item in response.get("items", []) if _bulk_item_failed(item)]
            raise ElasticsearchUnavailable(f"memory bulk indexing failed for {len(failed)} document(s)")
        return len(documents)

    def search_tree(self, request: TreeSearchRequest, query_vector: list[float] | None) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [
            ({"terms": {"knowledge_base_id": list(request.knowledge_base_ids)}} if request.knowledge_base_ids else {"term": {"knowledge_base_id": request.knowledge_base_id}}) if request.knowledge_base_id or request.knowledge_base_ids else {"match_all": {}},
            {"terms": {"tree_type": list(request.tree_types)}},
        ]
        if request.organization_id:
            filters.append({"term": {"organization_id": request.organization_id}})
        output: list[dict[str, Any]] = []
        branches = [("display", filters)]
        if request.include_protected and request.authorized_object_keys:
            branches.append(("protected", filters + [{"terms": {"auth_object_key": list(request.authorized_object_keys)}}]))
        branch_k = max(1, min(3, request.top_k))
        for visibility, branch_filters in branches:
            roots = self._search_nodes(request.query, query_vector, branch_filters + [{"term": {"node_type": "root"}}], request.top_k, visibility)
            for root in roots:
                groups = self._search_nodes(request.query, query_vector, branch_filters + [{"term": {"parent_id": str(root["node_id"])}}], branch_k, visibility)
                for group in groups:
                    leaves = self._search_nodes(request.query, query_vector, branch_filters + [{"term": {"parent_id": str(group["node_id"])}}], branch_k, visibility)
                    for leaf in leaves:
                        base_score = sum(float(value.get("_score", 0)) for value in (root, group, leaf))
                        fact_filters = branch_filters + [{"term": {"node_id": leaf["node_id"]}}, {"term": {"fact_status": "active"}}]
                        fact_hits = self._search(self.indices[("fact", visibility)], "fact_text", request.query, query_vector, fact_filters, request.top_k)
                        common = {"tree": {"tree_id": root["tree_id"], "tree_type": root["tree_type"], "subject_key": root["subject_key"]}, "path": [self._path_node(root), self._path_node(group), self._path_node(leaf)], "leaf_node_id": leaf["node_id"], "visibility": visibility, "degraded": query_vector is None}
                        if not fact_hits:
                            output.append({**common, "fact": None, "score": base_score})
                        for fact_hit in fact_hits:
                            output.append({**common, "fact": fact_hit["source"], "score": fact_hit["score"] + base_score})
        output.sort(key=lambda item: item["score"], reverse=True)
        return output[: request.top_k]

    def _best_child(self, query: str, vector: list[float] | None, filters: list[dict[str, Any]], parent_id: str) -> dict[str, Any] | None:
        values = self._search_nodes(query, vector, filters + [{"term": {"parent_id": parent_id}}], 1)
        return values[0] if values else None

    def _search_nodes(self, query: str, vector: list[float] | None, filters: list[dict[str, Any]], size: int, visibility: str = "display") -> list[dict[str, Any]]:
        return [{**hit["source"], "_score": hit["score"]} for hit in self._search(self.indices[("node", visibility)], "summary", query, vector, filters, size)]

    def _search(self, index: str, field: str, query: str, vector: list[float] | None, filters: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"index": index, "query": {"bool": {"should": [{"match": {field: {"query": query}}}], "minimum_should_match": 0, "filter": filters}}, "size": size, "_source": {"excludes": ["embedding"]}}
        if vector is not None:
            body["knn"] = {"field": "embedding", "query_vector": vector, "k": size, "num_candidates": max(size * 4, 20), "filter": {"bool": {"filter": filters}}}
        try:
            response = self.client.search(**body)
        except Exception as exc:
            if _is_not_found(exc):
                return []
            raise ElasticsearchUnavailable("memory tree search failed") from exc
        return [{"source": dict(hit.get("_source") or {}), "score": float(hit.get("_score") or 0)} for hit in response.get("hits", {}).get("hits", [])]

    @staticmethod
    def _path_node(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in ("node_id", "node_type", "summary", "topic_key", "phase_key")}

    @staticmethod
    def _mapping(kind: str) -> dict[str, Any]:
        path = Path(__file__).resolve().parents[2] / "config" / "elasticsearch" / f"memory-{kind}s-index.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["mappings"]["properties"]["embedding"]["dims"] = settings.embedding_dims
        return value

    @staticmethod
    def _write_alias(read_alias: str) -> str:
        return read_alias[:-5] + "_write" if read_alias.endswith("_read") else read_alias + "_write"
