"""FormBrowserClient adapter that executes atomic operations on a desktop device."""

from __future__ import annotations

import base64
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.infrastructure.knowledge.device_auth import DeviceAuthClient, DeviceAuthError
from app.infrastructure.postgres.desktop_tasks import DesktopTaskStore
from app.infrastructure.web.form_browser_client import (
    FormBrowserError,
    FormBrowserUnavailable,
    FormLoginRequired,
)
from app.kernel.execution_context import current_execution_context


TERMINAL_FAILURES = {"failed", "expired", "needs_review"}
_owner_override: ContextVar[str | None] = ContextVar(
    "desktop_form_owner_override",
    default=None,
)


@contextmanager
def bind_desktop_form_owner(owner_user_id: str):
    token = _owner_override.set(str(owner_user_id or "").strip() or None)
    try:
        yield
    finally:
        _owner_override.reset(token)


def _execution_identity() -> tuple[str, str, str]:
    try:
        context = current_execution_context()
    except RuntimeError:
        owner_user_id = _owner_override.get()
        if not owner_user_id:
            raise FormBrowserUnavailable(
                "desktop form execution has no owner context",
                code="desktop_owner_missing",
            )
        return owner_user_id, "", ""
    return context.owner_user_id, context.task_id, context.step_id


class DesktopFormBrowserClient:
    """Implements the same method surface as ``FormBrowserClient``."""

    def __init__(
        self,
        store: DesktopTaskStore,
        device_auth: DeviceAuthClient,
        *,
        task_ttl_seconds: float = 300.0,
        poll_seconds: float = 1.0,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.store = store
        self.device_auth = device_auth
        self.task_ttl_seconds = max(5.0, float(task_ttl_seconds))
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def _device_id(self) -> str:
        owner_user_id, _, _ = _execution_identity()
        try:
            identity = self.device_auth.get_active_device(owner_user_id)
        except DeviceAuthError as exc:
            if exc.status_code == 404:
                raise FormBrowserUnavailable(
                    "no paired desktop device is available",
                    code="desktop_device_unavailable",
                ) from exc
            raise FormBrowserUnavailable(str(exc)) from exc
        if not identity.device_id:
            raise FormBrowserUnavailable(
                "desktop device identity is empty",
                code="desktop_device_unavailable",
            )
        return identity.device_id

    def _execute(
        self,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        side_effect: bool = False,
        timeout_seconds: float | None = None,
    ) -> Any:
        owner_user_id, task_id, step_id = _execution_identity()
        task = self.store.create_task(
            device_id=self._device_id(),
            operation=operation,
            request_payload=payload or {},
            ttl_seconds=self.task_ttl_seconds,
            agent_task_id=task_id,
            agent_step_id=step_id,
            side_effect=side_effect,
        )
        deadline = time.monotonic() + float(
            timeout_seconds or self.timeout_seconds
        )
        while time.monotonic() < deadline:
            current = self.store.get_task(task.desktop_task_id)
            if current is None:
                raise FormBrowserUnavailable(
                    "desktop task disappeared",
                    code="desktop_task_missing",
                )
            if current.status == "completed":
                return current.result or {}
            if current.status == "waiting_login":
                if operation == "open":
                    result = current.result or {}
                    return {
                        "login_required": True,
                        "final_url": result.get("final_url") or payload.get("url") if payload else "",
                        "session_id": result.get("session_id") or task.request_payload.get("session_id") or "",
                    }
                raise FormLoginRequired(
                    current.error_message
                    or "the document needs the owner to sign in"
                )
            if current.status in TERMINAL_FAILURES:
                raise FormBrowserError(
                    current.error_message
                    or f"desktop form task {current.status}",
                    code=current.error_code or f"desktop_task_{current.status}",
                )
            time.sleep(self.poll_seconds)
        error_type = FormBrowserError if side_effect else FormBrowserUnavailable
        self.store.complete_task(
            desktop_task_id=task.desktop_task_id,
            device_id=task.device_id,
            status="needs_review" if side_effect else "failed",
            result={},
            error_code="desktop_task_timeout",
            error_message="desktop form task did not finish before the timeout",
            side_effect_state="unknown" if side_effect else "none",
        )
        raise error_type(
            "desktop form task did not finish before the timeout",
            code="desktop_task_timeout",
        )

    def create_session(self) -> str:
        result = self._execute("create_session")
        session_id = str((result or {}).get("session_id") or "")
        if not session_id:
            raise FormBrowserError("desktop form browser did not return a session id")
        return session_id

    def close_session(self, session_id: str) -> None:
        self._execute("close_session", {"session_id": session_id})

    def open(self, session_id: str, url: str) -> dict[str, Any]:
        return self._execute("open", {"session_id": session_id, "url": url})

    def read_grid(self, session_id: str) -> dict[str, Any]:
        return self._execute("read_grid", {"session_id": session_id})

    def write_grid(
        self, session_id: str, start_cell: str, values: list[list[str]]
    ) -> dict[str, Any]:
        return self._execute(
            "write_grid",
            {
                "session_id": session_id,
                "start_cell": start_cell,
                "values": values,
            },
            side_effect=True,
        )

    def clear_range(self, session_id: str, span: str) -> None:
        self._execute(
            "clear_range",
            {"session_id": session_id, "range": span},
            side_effect=True,
        )

    def read_form(self, session_id: str) -> dict[str, Any]:
        return self._execute("read_form", {"session_id": session_id})

    def fill_form(self, session_id: str, values: dict[str, str]) -> dict[str, Any]:
        return self._execute(
            "fill_form",
            {"session_id": session_id, "values": values},
            side_effect=True,
        )

    def submit_form(self, session_id: str, ref: str = "") -> dict[str, Any]:
        return self._execute(
            "submit_form",
            {"session_id": session_id, "ref": ref},
            side_effect=True,
        )

    def screenshot(self, session_id: str) -> bytes:
        result = self._execute("screenshot", {"session_id": session_id})
        encoded = str((result or {}).get("screenshot_base64") or "")
        if not encoded:
            raise FormBrowserError("desktop form browser did not return a screenshot")
        try:
            return base64.b64decode(encoded)
        except ValueError as exc:
            raise FormBrowserError("desktop screenshot is invalid") from exc

    def send_input(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._execute(
            "send_input",
            {"session_id": session_id, "input": payload},
            side_effect=True,
        )
