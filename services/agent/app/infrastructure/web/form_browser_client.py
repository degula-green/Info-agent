"""HTTP client for the form-browser sidecar.

Same shape as the renderer client: base URL plus a bearer token, urllib under
the hood, and ``_request`` as the test seam. The browser itself never runs in
the Agent process.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class FormBrowserError(RuntimeError):
    """Base error; ``classification`` is what the kernel reads."""

    classification = "permanent_error"
    code = "form_browser_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class FormBrowserUnavailable(FormBrowserError):
    """Transport-level failure: the same call may work next time."""

    classification = "retryable_error"
    code = "form_browser_unavailable"


class FormLoginRequired(FormBrowserError):
    """The document refused edits until the owner signs in.

    Not an error to retry: the Task has to hand control back to the owner.
    """

    classification = "permanent_error"
    code = "form_login_required"


class FormBrowserClient:
    """Calls the sidecar; ``_request`` is the test seam."""

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str = "",
        timeout_seconds: float = 180.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.api_token = str(api_token or "").strip()
        self.timeout_seconds = float(timeout_seconds)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        raw: bool = False,
    ) -> Any:
        if not self.base_url:
            raise FormBrowserUnavailable(
                "form browser is not configured", code="form_browser_not_configured"
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            detail = self._decode(payload)
            code = ""
            if isinstance(detail, dict):
                code = str(detail.get("code") or "")
            if code == "login_required":
                raise FormLoginRequired(
                    self._message(detail, "the document needs the owner to sign in")
                ) from exc
            if exc.code == 429 or exc.code >= 500:
                raise FormBrowserUnavailable(
                    f"form browser failed ({exc.code})",
                    code=code or "form_browser_http_error",
                ) from exc
            raise FormBrowserError(
                self._message(detail, f"form browser rejected ({exc.code})"),
                code=code or "form_browser_http_error",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FormBrowserUnavailable(f"form browser request failed: {exc}") from exc
        return payload if raw else self._decode(payload)

    @staticmethod
    def _decode(payload: bytes) -> Any:
        try:
            return json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except ValueError as exc:
            raise FormBrowserError(
                "form browser returned invalid JSON", code="invalid_json"
            ) from exc

    @staticmethod
    def _message(detail: Any, fallback: str) -> str:
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("detail")
            if message:
                return str(message)
        return fallback

    def create_session(self) -> str:
        body = self._request("POST", "/sessions")
        session_id = str((body or {}).get("session_id") or "")
        if not session_id:
            raise FormBrowserError("form browser did not return a session id")
        return session_id

    def close_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    def open(self, session_id: str, url: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/open", {"url": url})

    def read_grid(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id}/grid")

    def write_grid(
        self, session_id: str, start_cell: str, values: list[list[str]]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/sessions/{session_id}/grid/write",
            {"start_cell": start_cell, "values": values},
        )

    def clear_range(self, session_id: str, span: str) -> None:
        self._request("POST", f"/sessions/{session_id}/grid/clear", {"range": span})

    def screenshot(self, session_id: str) -> bytes:
        return self._request("GET", f"/sessions/{session_id}/screenshot", raw=True)
