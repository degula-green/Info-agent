from __future__ import annotations

from typing import Any

from app.application.ports import KnowledgeSource
from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, join_url, with_query


class KnowledgeSourceUnavailable(RuntimeError):
    pass


class Module2KnowledgeClient(KnowledgeSource):
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
    ) -> dict[str, Any]:
        if not self.base_url:
            raise KnowledgeSourceUnavailable("RAG_KNOWLEDGE_BASE_URL is not configured")
        try:
            value = self.http.request(
                "GET",
                with_query(
                    join_url(self.base_url, path),
                    content_version=content_version,
                    acl_version=acl_version,
                    content_variant=content_variant,
                    purpose=purpose,
                ),
                token=self.token,
                headers={"X-Caller-Service": "rag"},
                timeout=settings.knowledge_timeout_seconds,
            ).json()
        except IntegrationError as exc:
            raise KnowledgeSourceUnavailable("module 2 source request failed") from exc
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
    ) -> dict[str, Any]:
        return self._get(
            f"/internal/knowledge/{knowledge_item_id}/content",
            content_version=content_version,
            acl_version=acl_version,
            content_variant=content_variant,
            purpose=purpose,
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
