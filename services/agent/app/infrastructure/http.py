"""Small stdlib HTTP adapter for internal service calls.

Mirrors ``services/rag/app/infrastructure/http.py`` so the agent service keeps
the same error shape and the same "never echo remote bodies" rule without
adding a runtime dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class IntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.error_code = error_code


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError("remote response is not valid JSON") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> HttpResult:
        request_headers = {"Accept": "application/json", **(headers or {})}
        payload: bytes | None = None
        if body is not None:
            if isinstance(body, bytes):
                payload = body
            elif isinstance(body, str):
                payload = body.encode("utf-8")
            else:
                payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", content_type)
        if token:
            request_headers.setdefault("Authorization", f"Bearer {token}")
        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
                result = HttpResult(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            # Never surface remote bodies: they may carry credentials or content.
            # Only the short machine-readable code is carried forward.
            raise IntegrationError(
                f"remote HTTP request failed ({exc.code})",
                status=exc.code,
                retryable=retryable,
                error_code=_error_code(exc),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IntegrationError("remote HTTP request failed", retryable=True) from exc
        if result.status < 200 or result.status >= 300:
            raise IntegrationError(
                f"remote HTTP request failed ({result.status})",
                status=result.status,
                retryable=result.status >= 500,
            )
        return result


def join_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{suffix}"


def _error_code(exc: urllib.error.HTTPError) -> str | None:
    """Reads ``error`` / ``code`` from an error body without carrying it further."""

    try:
        raw = exc.read() or b""
    except Exception:  # noqa: BLE001 - a missing body must not mask the HTTP error
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("error") or payload.get("code")
    if not value:
        return None
    code = "".join(character for character in str(value)[:64] if character.isprintable())
    return code or None


def with_query(url: str, **params: Any) -> str:
    values = {key: str(value) for key, value in params.items() if value is not None}
    if not values:
        return url
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(values)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
