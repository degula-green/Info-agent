"""Knowledge internal API client: the conversation snapshot.

The Agent never reads the Knowledge database and never touches platform tokens.
The snapshot is the only interface left: the Agent used to write calendar events
through this client, but that surface was retired when the schedule and the to-do
were merged into the Agent's own ledger.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Protocol

from app.infrastructure.http import HttpClient, IntegrationError, join_url, with_query

SNAPSHOT_PATH = "/api/knowledge/v1/internal/agent/conversation-snapshot"
PERSON_RESOLVE_PATH = "/api/knowledge/v1/internal/agent/people-resolve"
PERSON_PROFILE_PATH = "/api/knowledge/v1/internal/agent/people-profile"
ATTACHMENT_SEARCH_PATH = "/api/knowledge/v1/internal/agent/attachments"


class KnowledgeError(RuntimeError):
    """Base class for Knowledge integration failures."""

    code: str | None = None
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)


class KnowledgeUnavailable(KnowledgeError):
    """Transient failure: the event must not be acknowledged."""

    retryable = True


class KnowledgeItemNotFound(KnowledgeError):
    code = "knowledge_item_not_found"


class KnowledgeItemNotReady(KnowledgeError):
    """Privacy or permission is not ready yet; retrying later can succeed."""

    code = "knowledge_item_not_ready"
    retryable = True


class KnowledgeForbidden(KnowledgeError):
    code = "forbidden"


class KnowledgeRejected(KnowledgeError):
    """Explicit 4xx that retrying will not fix."""

    code = "request_rejected"



class KnowledgeClient(Protocol):
    def conversation_snapshot(self, knowledge_item_id: str) -> dict[str, Any]:
        ...

    def resolve_person(
        self,
        *,
        owner_user_id: str,
        name: str,
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        ...

    def person_profile(
        self,
        *,
        owner_user_id: str,
        relation_ids: list[str],
        organization_id: str = "",
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        ...

    def search_attachments(
        self,
        *,
        owner_user_id: str,
        name: str,
        limit: int = 50,
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        ...

    def open_attachment(
        self,
        *,
        owner_user_id: str,
        attachment_id: str,
        request_id: str = "",
        trace_id: str = "",
    ) -> bytes:
        ...


class HttpKnowledgeClient:
    """Talks to the Knowledge service over its internal, token-protected API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_seconds: float = 5.0,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.http = http or HttpClient()

    def _headers(self) -> dict[str, str]:
        return {"X-Caller-Service": "agent"}

    def conversation_snapshot(self, knowledge_item_id: str) -> dict[str, Any]:
        if not self.base_url:
            raise KnowledgeUnavailable("AGENT_KNOWLEDGE_BASE_URL is not configured")
        url = with_query(join_url(self.base_url, SNAPSHOT_PATH), knowledge_item_id=knowledge_item_id)
        try:
            result = self.http.request(
                "GET",
                url,
                headers=self._headers(),
                token=self.token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise self._snapshot_transport_error(exc) from exc
        body = _decode(result)
        if not isinstance(body, dict):
            raise KnowledgeUnavailable("snapshot response is not a JSON object")
        return body

    def resolve_person(
        self,
        *,
        owner_user_id: str,
        name: str,
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        if not self.base_url:
            raise KnowledgeUnavailable("AGENT_KNOWLEDGE_BASE_URL is not configured")
        headers = self._headers()
        headers["X-User-ID"] = str(owner_user_id or "").strip()
        if request_id:
            headers["X-Request-ID"] = request_id
        if trace_id:
            headers["X-Trace-ID"] = trace_id
        try:
            result = self.http.request(
                "POST",
                join_url(self.base_url, PERSON_RESOLVE_PATH),
                body={"name": name},
                headers=headers,
                token=self.token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise self._snapshot_transport_error(exc) from exc
        body = _decode(result)
        if not isinstance(body, dict):
            raise KnowledgeUnavailable("person response is not a JSON object")
        return body

    def person_profile(
        self,
        *,
        owner_user_id: str,
        relation_ids: list[str],
        organization_id: str = "",
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        if not self.base_url:
            raise KnowledgeUnavailable("AGENT_KNOWLEDGE_BASE_URL is not configured")
        headers = self._headers()
        headers["X-User-ID"] = str(owner_user_id or "").strip()
        if request_id:
            headers["X-Request-ID"] = request_id
        if trace_id:
            headers["X-Trace-ID"] = trace_id
        try:
            result = self.http.request(
                "POST",
                join_url(self.base_url, PERSON_PROFILE_PATH),
                body={
                    "relation_ids": list(relation_ids),
                    "organization_id": organization_id,
                },
                headers=headers,
                token=self.token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise self._snapshot_transport_error(exc) from exc
        body = _decode(result)
        if not isinstance(body, dict):
            raise KnowledgeUnavailable("person profile response is not a JSON object")
        return body

    def search_attachments(
        self,
        *,
        owner_user_id: str,
        name: str,
        limit: int = 50,
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        """Collected documents whose file name matches, scoped to this user."""

        if not self.base_url:
            raise KnowledgeUnavailable("AGENT_KNOWLEDGE_BASE_URL is not configured")
        headers = self._headers()
        headers["X-User-ID"] = str(owner_user_id or "").strip()
        if request_id:
            headers["X-Request-ID"] = request_id
        if trace_id:
            headers["X-Trace-ID"] = trace_id
        url = with_query(
            join_url(self.base_url, ATTACHMENT_SEARCH_PATH),
            name=name,
            limit=max(int(limit), 1),
        )
        try:
            result = self.http.request(
                "GET",
                url,
                headers=headers,
                token=self.token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise self._snapshot_transport_error(exc) from exc
        body = _decode(result)
        if not isinstance(body, dict):
            raise KnowledgeUnavailable("attachment search response is not a JSON object")
        return body

    def open_attachment(
        self,
        *,
        owner_user_id: str,
        attachment_id: str,
        request_id: str = "",
        trace_id: str = "",
    ) -> bytes:
        """The original bytes of a collected attachment the user may read."""

        if not self.base_url:
            raise KnowledgeUnavailable("AGENT_KNOWLEDGE_BASE_URL is not configured")
        headers = self._headers()
        headers["X-User-ID"] = str(owner_user_id or "").strip()
        if request_id:
            headers["X-Request-ID"] = request_id
        if trace_id:
            headers["X-Trace-ID"] = trace_id
        path = f"{ATTACHMENT_SEARCH_PATH}/{urllib.parse.quote(str(attachment_id))}/content"
        try:
            result = self.http.request(
                "GET",
                join_url(self.base_url, path),
                headers=headers,
                token=self.token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise self._snapshot_transport_error(exc) from exc
        return result.body

    def _snapshot_transport_error(self, exc: IntegrationError) -> KnowledgeError:
        status = exc.status
        if status is None or exc.retryable:
            return KnowledgeUnavailable(str(exc))
        if status == 404:
            return KnowledgeItemNotFound("knowledge item was not found")
        if status == 409:
            return KnowledgeItemNotReady("knowledge item is not ready yet")
        if status in (401, 403):
            return KnowledgeForbidden("snapshot call is not authorized")
        return KnowledgeRejected(f"snapshot call was rejected ({status})")



def _decode(result) -> Any:
    try:
        return result.json()
    except IntegrationError as exc:
        raise KnowledgeUnavailable("remote response is not valid JSON") from exc
