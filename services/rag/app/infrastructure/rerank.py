from __future__ import annotations

from typing import Any

from app.application.retrieval.rerank import Reranker
from app.config import settings
from app.domain.models import SearchResult
from app.infrastructure.http import HttpClient, IntegrationError, join_url


class RerankUnavailable(RuntimeError):
    pass


class RemoteReranker(Reranker):
    def __init__(self, *, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        if not settings.rerank_api_base_url or not settings.rerank_api_key or not settings.rerank_model:
            raise RerankUnavailable("rerank provider is not configured")
        payload = {"model": settings.rerank_model, "query": query, "documents": [item.content for item in results], "top_n": len(results)}
        try:
            value = self.http.request("POST", join_url(settings.rerank_api_base_url, "/rerank"), body=payload, token=settings.rerank_api_key, timeout=settings.rerank_timeout_seconds).json()
        except IntegrationError as exc:
            raise RerankUnavailable("rerank provider request failed") from exc
        entries = value.get("results") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            raise RerankUnavailable("rerank provider returned an invalid response")
        ranked: list[SearchResult] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            index = int(entry.get("index", -1))
            if 0 <= index < len(results):
                result = results[index]
                result.rerank_score = float(entry.get("relevance_score", entry.get("score", 0.0)))
                ranked.append(result)
        return ranked or results
