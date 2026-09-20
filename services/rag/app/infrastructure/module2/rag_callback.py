from __future__ import annotations

from typing import Any

from app.config import settings
from app.infrastructure.http import HttpClient, join_url


class KnowledgeRAGCallbackClient:
    """Reliable-delivery target for RAG status callbacks to Knowledge."""

    def __init__(self, *, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def send(self, payload: dict[str, Any]) -> None:
        if not settings.knowledge_callback_enabled:
            return
        item_id = str(payload.get("knowledge_item_id") or "")
        if not settings.knowledge_base_url or not item_id:
            raise RuntimeError("Knowledge callback is not configured")
        path = settings.knowledge_callback_path.format(knowledge_item_id=item_id)
        self.http.request("POST", join_url(settings.knowledge_base_url, path), body=payload, token=settings.knowledge_api_token, headers={"X-Caller-Service": "rag"}, timeout=settings.knowledge_timeout_seconds)
