"""Line-oriented streaming HTTP adapter for OpenAI-compatible SSE.

The regular ``HttpClient`` reads the whole body before returning, which is
correct for the one-shot path but cannot deliver first-token latency. This
adapter keeps the same error shape and the same "never echo remote bodies"
rule, while exposing a generator of decoded lines.

Cancellation is cooperative: a watcher thread calls ``should_cancel`` and closes
the connection when it turns true, which unblocks a read that is waiting on the
network. The generator then raises ``StreamCancelled`` instead of a transport
error so callers can tell a user cancel apart from a provider failure.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.parse
from typing import Any, Callable, Iterator, Mapping

from app.infrastructure.http import IntegrationError


class StreamCancelled(RuntimeError):
    """The caller asked to stop the in-flight stream."""


def stream_request(
    method: str,
    url: str,
    *,
    body: Any | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    token: str | None = None,
    content_type: str = "application/json",
    idle_timeout: float | None = None,
    total_timeout: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Yield decoded response lines from a long-lived HTTP response."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IntegrationError("remote stream URL is invalid", retryable=False)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    idle = max(0.1, float(idle_timeout if idle_timeout is not None else timeout))
    socket_timeout = max(0.1, min(float(timeout), idle))
    connection = connection_type(parsed.hostname, parsed.port, timeout=socket_timeout)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    request_headers = {"Accept": "text/event-stream", **(headers or {})}
    payload: bytes | None = None
    if body is not None:
        if isinstance(body, bytes):
            payload = body
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        request_headers.setdefault("Content-Type", content_type)
    if token:
        request_headers.setdefault("Authorization", f"Bearer {token}")

    cancelled = threading.Event()
    stop_watcher = threading.Event()
    watcher: threading.Thread | None = None
    if should_cancel is not None:
        def watch() -> None:
            while not stop_watcher.wait(0.25):
                try:
                    if should_cancel():
                        cancelled.set()
                        try:
                            connection.close()
                        except Exception:  # noqa: BLE001 - best effort unblock
                            pass
                        return
                except Exception:  # noqa: BLE001 - a probe failure must not kill the stream
                    continue

        watcher = threading.Thread(
            target=watch, name="agent-stream-cancel", daemon=True
        )
        watcher.start()

    deadline = (
        time.monotonic() + float(total_timeout)
        if total_timeout is not None and float(total_timeout) > 0
        else None
    )
    try:
        try:
            connection.request(method.upper(), path, body=payload, headers=request_headers)
            response = connection.getresponse()
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            if cancelled.is_set():
                raise StreamCancelled("stream cancelled") from exc
            raise IntegrationError("remote stream request failed", retryable=True) from exc
        if response.status < 200 or response.status >= 300:
            raw = response.read()
            code = _error_code_from_body(raw)
            detail = f" ({response.status} {code})" if code else f" ({response.status})"
            raise IntegrationError(
                f"remote stream request failed{detail}",
                status=response.status,
                retryable=response.status == 429 or response.status >= 500,
                error_code=code,
            )
        while True:
            if cancelled.is_set():
                raise StreamCancelled("stream cancelled")
            if should_cancel is not None:
                try:
                    if should_cancel():
                        raise StreamCancelled("stream cancelled")
                except StreamCancelled:
                    raise
                except Exception:  # noqa: BLE001 - a probe failure is not a stream failure
                    pass
            if deadline is not None and time.monotonic() >= deadline:
                raise IntegrationError("remote stream exceeded total timeout", retryable=True)
            try:
                raw = response.readline()
            except (socket.timeout, TimeoutError) as exc:
                if cancelled.is_set():
                    raise StreamCancelled("stream cancelled") from exc
                raise IntegrationError("remote stream idle timeout", retryable=True) from exc
            except (http.client.HTTPException, OSError) as exc:
                if cancelled.is_set():
                    raise StreamCancelled("stream cancelled") from exc
                raise IntegrationError("remote stream interrupted", retryable=True) from exc
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace")
    finally:
        stop_watcher.set()
        if watcher is not None:
            watcher.join(timeout=0.5)
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing must never mask the real error
            pass


def _error_code_from_body(raw: bytes) -> str | None:
    try:
        payload = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("error") or payload.get("code")
    if isinstance(value, dict):
        value = value.get("code") or value.get("type")
    if not value:
        return None
    code = "".join(character for character in str(value)[:64] if character.isprintable())
    return code or None
