"""Knowledge internal API client: conversation snapshot and calendar writes.

The Agent never reads the Knowledge database and never touches platform tokens;
both interfaces are documented in ``Agent开发第2步-Calendar真实能力.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.infrastructure.http import HttpClient, IntegrationError, join_url, with_query

SNAPSHOT_PATH = "/api/knowledge/v1/internal/agent/conversation-snapshot"
CALENDAR_EVENT_PATH = "/api/knowledge/v1/internal/agent/calendar/events"


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


class CalendarNotBound(KnowledgeError):
    code = "calendar_not_bound"


class CalendarAuthorizationInvalid(KnowledgeError):
    code = "calendar_authorization_invalid"


class CalendarRejected(KnowledgeError):
    """Explicit 4xx rejection: permanent, the Task must fail."""

    code = "calendar_rejected"


class CalendarResultUnknown(KnowledgeError):
    """The write may or may not have landed: never retry blindly."""

    code = "calendar_result_unknown"


class KnowledgeClient(Protocol):
    def conversation_snapshot(self, knowledge_item_id: str) -> dict[str, Any]:
        ...

    def create_calendar_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
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

    def create_calendar_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise CalendarResultUnknown("AGENT_KNOWLEDGE_BASE_URL is not configured")
        url = join_url(self.base_url, CALENDAR_EVENT_PATH)
        try:
            result = self.http.request(
                "POST",
                url,
                body=dict(payload),
                headers=self._headers(),
                token=self.token,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            # A timeout or 5xx may still have created the event.
            raise self._calendar_transport_error(exc) from exc
        body = _decode(result)
        if not isinstance(body, dict) or not body.get("event_id"):
            raise CalendarResultUnknown("calendar response has no event_id")
        return body

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

    def _calendar_transport_error(self, exc: IntegrationError) -> KnowledgeError:
        status = exc.status
        code = exc.error_code
        if code == "calendar_not_bound":
            return CalendarNotBound("the owner has no usable calendar authorization")
        if code == "calendar_authorization_invalid":
            return CalendarAuthorizationInvalid("calendar authorization must be renewed")
        if status is None or exc.retryable:
            return CalendarResultUnknown(str(exc))
        return CalendarRejected(f"calendar call was rejected ({status})")


def _decode(result) -> Any:
    try:
        return result.json()
    except IntegrationError as exc:
        raise KnowledgeUnavailable("remote response is not valid JSON") from exc
