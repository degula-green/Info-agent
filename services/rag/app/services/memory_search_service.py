from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.config import settings
from app.domain.memory import TreeSearchRequest
from app.domain.models import AccessCheck
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.memory_elasticsearch import ElasticsearchMemoryStore
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository
from app.infrastructure.service1.authorization_client import AllowAllAuthorizationGateway, Service1AuthorizationClient


class TreeSearchService:
    def __init__(self, *, indexer: Any | None = None, repository: Any | None = None, embedding_provider: Any | None = None, authorization: Any | None = None) -> None:
        self.indexer = indexer or ElasticsearchMemoryStore()
        self.repository = repository or (PostgresRagRepository() if settings.database_url else InMemoryRagRepository())
        self.embedding_provider = embedding_provider or EmbeddingClient()
        self.authorization = authorization or (Service1AuthorizationClient() if settings.authz_base_url else AllowAllAuthorizationGateway())

    def search(self, request: TreeSearchRequest) -> dict[str, Any]:
        degraded: list[str] = []
        snapshot_id: str | None = None
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
            if fact:
                fact_evidence = [{**source, "visibility": raw_fact.get("visibility")} for source in evidence.get(str(fact["fact_id"]), [])]
                safe_sources.extend(self._authorize_sources(request, fact_evidence, snapshot_id))
            items.append({**row, "fact": fact, "sources": safe_sources, "degraded": bool(row.get("degraded") or degraded)})
        source_count = len({str(source.get("source_id") or source.get("attachment_id") or source.get("knowledge_item_id")) for item in items for source in item.get("sources", [])})
        return {"query": request.query, "items": items, "diagnostics": {"degraded": degraded, "result_count": len(items), "source_candidate_count": source_count}}

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
