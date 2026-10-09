from __future__ import annotations

from typing import Any

from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, with_query
from app.infrastructure.module2.urls import knowledge_api_url


class KnowledgeSourceUnavailable(RuntimeError):
    retryable = True
    code = "knowledge_source_unavailable"

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Module2KnowledgeClient:
    """Versioned HTTP back-source; it never reads module 2's database."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.knowledge_base_url).rstrip("/")
        self.token = token if token is not None else settings.knowledge_api_token
        self.http = http or HttpClient()

    def _get(
        self,
        path: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        content_variant: str | None = None,
        purpose: str | None = None,
        rag_job_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise KnowledgeSourceUnavailable("RAG_KNOWLEDGE_BASE_URL is not configured")
        try:
            value = self.http.request(
                "GET",
                with_query(
                    knowledge_api_url(self.base_url, path),
                    content_version=content_version,
                    acl_version=acl_version,
                    content_variant=content_variant,
                    purpose=purpose,
                ),
                token=self.token,
                headers={
                    "X-Caller-Service": "rag",
                    "X-RAG-Job-ID": rag_job_id,
                    "X-Trace-ID": trace_id,
                },
                timeout=settings.knowledge_timeout_seconds,
            ).json()
        except IntegrationError as exc:
            status = getattr(exc, "status", None)
            retryable = bool(getattr(exc, "retryable", True))
            if status is not None:
                # 409 means the Knowledge item exists but is not ready yet
                # (permission/classification/processing still in flight), so
                # the right behavior is a delayed retry rather than a terminal
                # failure. 404/400/403 are permanent for this job.
                retryable = status >= 500 or status in {408, 409, 425, 429}
            detail = f"status={status}" if status is not None else "status=transport"
            raise KnowledgeSourceUnavailable(
                f"module 2 source request failed ({detail})",
                retryable=retryable,
            ) from exc
        if not isinstance(value, dict):
            raise KnowledgeSourceUnavailable("module 2 returned an invalid source response")
        return value

    def get_knowledge(
        self,
        knowledge_item_id: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/internal/knowledge/{knowledge_item_id}",
            content_version=content_version,
            acl_version=acl_version,
            purpose=purpose,
        )

    def get_content(
        self,
        knowledge_item_id: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        content_variant: str | None = None,
        purpose: str | None = None,
        rag_job_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/internal/knowledge/{knowledge_item_id}/content",
            content_version=content_version,
            acl_version=acl_version,
            content_variant=content_variant,
            purpose=purpose,
            rag_job_id=rag_job_id,
            trace_id=trace_id,
        )

    def get_attachment(
        self,
        attachment_id: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/internal/attachments/{attachment_id}",
            content_version=content_version,
            acl_version=acl_version,
            purpose=purpose,
        )
