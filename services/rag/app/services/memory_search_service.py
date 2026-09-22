from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.config import settings
from app.domain.memory import TreeSearchRequest
from app.domain.models import SearchRequest, SearchResult
from app.application.retrieval.query_planner import plan_query
from app.infrastructure.elasticsearch import ElasticsearchChunkStore
from app.domain.models import AccessCheck
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.memory_elasticsearch import ElasticsearchMemoryStore
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository
from app.infrastructure.service1.authorization_client import AllowAllAuthorizationGateway, Service1AuthorizationClient


class TreeSearchService:
    def __init__(self, *, indexer: Any | None = None, repository: Any | None = None, embedding_provider: Any | None = None, authorization: Any | None = None, chunk_store: Any | None = None) -> None:
        self.indexer = indexer or ElasticsearchMemoryStore()
        self.repository = repository or (PostgresRagRepository() if settings.database_url else InMemoryRagRepository())
        self.embedding_provider = embedding_provider or EmbeddingClient()
        self.authorization = authorization or (Service1AuthorizationClient() if settings.authz_base_url else AllowAllAuthorizationGateway())
        self.chunk_store = chunk_store

    def search(self, request: TreeSearchRequest) -> dict[str, Any]:
        degraded: list[str] = []
        snapshot_id: str | None = None
        explicit_tree_types = request.tree_types is not None
        plan = plan_query(SearchRequest(
            query=request.query, user_id=request.user_id, organization_id=request.organization_id,
            knowledge_base_id=request.knowledge_base_id, knowledge_base_ids=request.knowledge_base_ids,
            entry="ai", occurred_after=request.occurred_after, occurred_before=request.occurred_before,
        ))
        selected_tree_types = tuple(request.tree_types or (plan.preferred_tree_type,))
        effective_after = request.occurred_after or plan.time_after
        effective_before = request.occurred_before or plan.time_before
        request = replace(request, tree_types=selected_tree_types, occurred_after=effective_after, occurred_before=effective_before)
        try:
            query_vector = self.embedding_provider.embed([request.query])[0]
        except Exception:
            query_vector = None
            degraded.append("vector_unavailable")
        if request.include_protected:
            try:
                scope = self.authorization.search_scope(
                    user_id=request.user_id, organization_id=request.organization_id,
                    resource_parts=("original", "content"), knowledge_base_id=request.knowledge_base_id,
                    knowledge_base_ids=request.knowledge_base_ids,
                )
                keys = tuple(key for key in scope.object_keys if key != "*") if scope.available else ()
                snapshot_id = scope.snapshot_id
                request = replace(request, authorized_object_keys=keys)
                if not scope.available:
                    degraded.append("protected_scope_unavailable")
            except Exception:
                request = replace(request, authorized_object_keys=())
                degraded.append("protected_scope_unavailable")
        rows = self.indexer.search_tree(request, query_vector)
        evidence = self.repository.evidence_for_facts([str(row["fact"]["fact_id"]) for row in rows if row.get("fact")])
        sources_by_node = self.repository.sources_for_nodes([str(row["leaf_node_id"]) for row in rows if row.get("leaf_node_id")])
        items = []
        for row in rows:
            raw_fact = row.get("fact")
            fact = {key: value for key, value in raw_fact.items() if key not in {"embedding", "auth_object_key"}} if raw_fact else None
            if raw_fact and not self._authorize_fact(request, raw_fact, snapshot_id):
                continue
            sources = list(sources_by_node.get(str(row.get("leaf_node_id")), []))
            if row.get("visibility") == "protected":
                allowed = set(request.authorized_object_keys)
                sources = [source for source in sources if source.get("auth_object_key") in allowed]
            sources = self._authorize_sources(request, sources, snapshot_id)
            safe_sources = [{key: value for key, value in source.items() if key != "auth_object_key"} for source in sources]
            direct_chunks: list[dict[str, Any]] = []
            if fact:
                fact_evidence = []
                for source in evidence.get(str(fact["fact_id"]), []):
                    protected = str(raw_fact.get("visibility") or "display") == "protected"
                    attachment_id = source.get("attachment_id") or raw_fact.get("attachment_id")
                    auth_object_key = str(raw_fact.get("auth_object_key") or source.get("auth_object_key") or "")
                    uses_attachment_acl = bool(
                        attachment_id and (protected or auth_object_key.startswith("attachment_content:"))
                    )
                    auth_type = "attachment" if uses_attachment_acl else "knowledge_item"
                    auth_part = "content" if uses_attachment_acl else "display"
                    auth_id = str(
                        attachment_id
                        if uses_attachment_acl
                        else source.get("knowledge_item_id") or raw_fact.get("knowledge_item_id") or ""
                    )
                    fact_evidence.append({**source, "visibility": raw_fact.get("visibility"), "auth_object_key": auth_object_key,
                                          "auth_resource_type": auth_type, "auth_resource_part": auth_part, "auth_resource_id": auth_id,
                                          "conversation_group_id": source.get("conversation_key"), "message_id": source.get("message_id"),
                                          "sent_at": source.get("observed_at"),
                                          **{key: value for key, value in (source.get("source_metadata") or {}).items() if key in {"file_name", "mime_type", "platform", "conversation_id"} and value},
                                          "part_kind": (source.get("source_locator") or {}).get("part_kind"),
                                          "file_name": source.get("file_name") or (source.get("source_locator") or {}).get("file_name"),
                                          "mime_type": source.get("mime_type") or (source.get("source_locator") or {}).get("mime_type")})
                authorized_direct = self._authorize_sources(request, fact_evidence, snapshot_id)
                safe_sources.extend([{key: value for key, value in source.items() if key != "auth_object_key"} for source in authorized_direct])
                direct_chunks = [{**source, "relation": "direct_evidence", "fact_id": fact.get("fact_id"), "tree_path": row.get("path")} for source in authorized_direct if str(source.get("content") or "").strip()]
            context_chunks = self._expand_context(request, direct_chunks, snapshot_id, degraded)
            items.append({**row, "fact": fact, "sources": safe_sources, "context_chunks": context_chunks,
                          "degraded": bool(row.get("degraded") or degraded)})
        source_count = len({str(source.get("source_id") or source.get("attachment_id") or source.get("knowledge_item_id")) for item in items for source in item.get("sources", [])})
        return {
            "query": request.query, "items": items,
            "routing": {"tree_types": list(selected_tree_types), "explicit": explicit_tree_types, "reason": plan.routing_reason, "execution_path": "session_tree" if selected_tree_types == ("session",) else "entity_tree"},
            "context_diagnostics": {"time_resolved": plan.time_resolved, "time_unresolved": plan.time_unresolved, "occurred_after": effective_after, "occurred_before": effective_before, "expanded": any(item.get("context_chunks") for item in items), "direct_only_without_time": any(item.get("context_chunks") and not any(chunk.get("relation") == "neighbor" for chunk in item["context_chunks"]) for item in items)},
            "diagnostics": {"degraded": degraded, "result_count": len(items), "source_candidate_count": source_count, "routing": {"tree_types": list(selected_tree_types), "reason": plan.routing_reason}},
        }

    def _expand_context(self, request: TreeSearchRequest, direct_chunks: list[dict[str, Any]], snapshot_id: str | None, degraded: list[str]) -> list[dict[str, Any]]:
        if not direct_chunks:
            return []
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in direct_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id and chunk_id not in seen:
                seen.add(chunk_id)
                output.append({key: value for key, value in chunk.items() if key != "auth_object_key"})
        if not request.expand_context:
            return output[:request.context_limit]
        store = self.chunk_store
        if store is None:
            try:
                store = ElasticsearchChunkStore()
            except Exception:
                degraded.append("context_store_unavailable")
                return output[:request.context_limit]
        for anchor in direct_chunks:
            conversation_id = anchor.get("conversation_group_id") or anchor.get("conversation_key")
            sent_at = anchor.get("sent_at") or anchor.get("observed_at")
            if not conversation_id or not sent_at:
                continue
            try:
                neighbors = store.search_context_chunks(
                    conversation_id=str(conversation_id), sent_at=str(sent_at),
                    knowledge_base_ids=request.knowledge_base_ids,
                    organization_id=request.organization_id,
                    window_minutes=max(1, request.context_window_minutes), size=max(request.context_limit, 12),
                    authorized_object_keys=request.authorized_object_keys, include_protected=request.include_protected,
                )
            except Exception:
                degraded.append("context_search_failed")
                continue
            candidate_sources = []
            for result in neighbors:
                source = dict(result.source)
                source.update({"content": result.content, "chunk_id": result.chunk_id, "relation": "neighbor",
                               "visibility": source.get("visibility") or source.get("content_visibility") or "display",
                               "fact_id": anchor.get("fact_id"), "tree_path": anchor.get("tree_path")})
                candidate_sources.append(source)
            for allowed in self._authorize_sources(request, candidate_sources, snapshot_id):
                value = {key: value for key, value in allowed.items() if key != "auth_object_key"}
                key = str(value.get("message_id") or value.get("chunk_id") or "")
                # One message can contain several chunks; retain the first body
                # while allowing direct evidence to win over a neighbor copy.
                if key and key in {str(item.get("message_id") or item.get("chunk_id") or "") for item in output}:
                    continue
                if str(value.get("chunk_id") or "") in seen:
                    continue
                seen.add(str(value.get("chunk_id") or ""))
                output.append(value)
                if len(output) >= request.context_limit:
                    return output[:request.context_limit]
        return output[:request.context_limit]

    def _authorize_sources(self, request: TreeSearchRequest, sources: list[dict[str, Any]], snapshot_id: str | None) -> list[dict[str, Any]]:
        checks: list[AccessCheck] = []
        for source in sources:
            protected = str(source.get("visibility") or "display") == "protected"
            attachment_id = source.get("attachment_id")
            # Attachment-backed sources carry the authoritative ACL object even
            # when their indexed visibility is display (for example a public
            # preview of a conversation attachment). Reusing that object keeps
            # tree authorization consistent with chunk authorization.
            auth_object_key = str(source.get("auth_object_key") or "")
            if attachment_id and (protected or auth_object_key.startswith("attachment_content:")):
                checks.append(AccessCheck("attachment", "content", str(attachment_id)))
            else:
                checks.append(AccessCheck("knowledge_item", "display", str(source.get("knowledge_item_id") or source.get("source_resource_id") or "")))
        if not checks:
            return []
        try:
            decisions = self.authorization.check_batch(
                user_id=request.user_id, organization_id=request.organization_id,
                checks=checks, snapshot_id=snapshot_id,
            )
        except Exception:
            return []
        if len(decisions) != len(sources):
            return []
        return [source for source, allowed in zip(sources, decisions) if bool(allowed)]

    def _authorize_fact(self, request: TreeSearchRequest, fact: dict[str, Any], snapshot_id: str | None) -> bool:
        protected = str(fact.get("visibility") or "display") == "protected"
        auth_object_key = str(fact.get("auth_object_key") or "")
        if fact.get("attachment_id") and (protected or auth_object_key.startswith("attachment_content:")):
            check = AccessCheck("attachment", "content", str(fact["attachment_id"]))
        else:
            check = AccessCheck("knowledge_item", "display", str(fact.get("knowledge_item_id") or fact.get("fact_id") or ""))
        try:
            decisions = self.authorization.check_batch(
                user_id=request.user_id, organization_id=request.organization_id,
                checks=[check], snapshot_id=snapshot_id,
            )
        except Exception:
            return False
        return len(decisions) == 1 and bool(decisions[0])


_service: TreeSearchService | None = None


def get_tree_search_service() -> TreeSearchService:
    global _service
    if _service is None:
        _service = TreeSearchService()
    return _service
