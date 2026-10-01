from __future__ import annotations

from typing import Any, Protocol

from app.infrastructure.http import HttpClient, IntegrationError, join_url


class RAGClient(Protocol):
    def search_sources(
        self,
        body: dict[str, Any],
        *,
        user_id: str,
        organization_id: str | None,
        request_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        ...

    def search_content(
        self,
        body: dict[str, Any],
        *,
        user_id: str,
        organization_id: str | None,
        request_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        ...


class RAGUnavailable(RuntimeError):
    pass


class HttpRAGClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 30.0,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.service_token = service_token
        self.timeout_seconds = timeout_seconds
        self.http = http or HttpClient()

    def search_sources(
        self,
        body: dict[str, Any],
        *,
        user_id: str,
        organization_id: str | None,
        request_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        return self._post(
            "/api/v1/search/sources",
            body,
            user_id=user_id,
            organization_id=organization_id,
            request_id=request_id,
            trace_id=trace_id,
        )

    def search_content(
        self,
        body: dict[str, Any],
        *,
        user_id: str,
        organization_id: str | None,
        request_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        return self._post(
            "/api/v1/search/content",
            body,
            user_id=user_id,
            organization_id=organization_id,
            request_id=request_id,
            trace_id=trace_id,
        )

    def _post(self, path: str, body: dict[str, Any], **identity: Any) -> dict[str, Any]:
        if not self.base_url:
            raise RAGUnavailable("AGENT_RAG_BASE_URL is not configured")
        headers = {
            "X-Agent-Service-Token": self.service_token,
            "X-User-ID": identity["user_id"],
            "X-Request-ID": identity["request_id"],
            "X-Trace-ID": identity["trace_id"],
        }
        if identity.get("organization_id"):
            headers["X-Organization-ID"] = str(identity["organization_id"])
        try:
            result = self.http.request(
                "POST",
                join_url(self.base_url, path),
                body=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise RAGUnavailable("RAG request failed") from exc
        try:
            value = result.json()
        except IntegrationError as exc:
            raise RAGUnavailable("RAG response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise RAGUnavailable("RAG response is not a JSON object")
        return value
