"""HTTP client for the TypeSafe System One endpoint.

Laya's local sidecar and Jev's cloud API (through AIHubMix) expose the same
``POST /v1/systemone`` contract, so one client serves both. The extra concern
here is bounded retry: the cloud path runs over the public internet and may
answer 429/529 or drop the connection, while 401/402/403/404/422 describe a
request that retrying cannot fix.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any, Mapping, Protocol

from app.infrastructure.http import HttpClient, IntegrationError, join_url

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)
# A server-provided delay is honoured, but a runaway Retry-After must not park
# a worker for minutes; the Task lease and the execution budget are shorter.
MAX_RETRY_DELAY_SECONDS = 8.0


class LayaError(RuntimeError):
    classification = "permanent_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.classification = "retryable_error" if retryable else "permanent_error"


# The protocol predates the Jev provider; both names describe the same failure.
SystemOneError = LayaError


class LayaClient(Protocol):
    model: str

    def predict(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        max_len: int | None = None,
        head_max_len: int | None = None,
    ) -> dict[str, Any]:
        ...


SystemOneClient = LayaClient


class HttpSystemOneClient:
    """Calls the TypeSafe-compatible endpoint exposed by a sidecar or gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "multilingual",
        timeout_seconds: float = 5.0,
        http: HttpClient | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: Sequence[float] = DEFAULT_RETRY_BACKOFF_SECONDS,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.http = http or HttpClient()
        self.max_retries = max(int(max_retries), 0)
        self.retry_backoff_seconds = tuple(
            max(float(value), 0.0) for value in retry_backoff_seconds
        )
        self._sleep = sleep or time.sleep

    def predict(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        max_len: int | None = None,
        head_max_len: int | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise LayaError("System One base URL is required", retryable=True)

        payload: dict[str, Any] = {
            "state": state,
            "questions": questions,
            "model": self.model,
        }
        # Laya-only tuning knobs. The Jev path leaves these unset, and the
        # cloud API rejects unknown fields instead of ignoring them.
        if max_len is not None:
            payload["max_len"] = max_len
        if head_max_len is not None:
            payload["head_max_len"] = head_max_len

        response = self._request(payload)
        try:
            body = response.json()
        except IntegrationError as exc:
            raise LayaError("System One response is not valid JSON") from exc
        if not isinstance(body, Mapping):
            raise LayaError("System One response is not a JSON object")
        return dict(body)

    # -- internals ---------------------------------------------------------

    def _request(self, payload: dict[str, Any]):
        last_error: IntegrationError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.http.request(
                    "POST",
                    systemone_url(self.base_url),
                    body=payload,
                    token=self.api_key or None,
                    timeout=self.timeout_seconds,
                )
            except IntegrationError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.max_retries:
                    raise LayaError(str(exc), retryable=exc.retryable) from exc
                self._sleep(self._delay_for(attempt, exc))
        # The loop either returns or raises; this guards a future refactor.
        raise LayaError(
            str(last_error) if last_error else "System One request failed",
            retryable=True,
        )

    def _delay_for(self, attempt: int, exc: IntegrationError) -> float:
        if exc.retry_after is not None:
            return min(float(exc.retry_after), MAX_RETRY_DELAY_SECONDS)
        if not self.retry_backoff_seconds:
            return 0.0
        index = min(attempt, len(self.retry_backoff_seconds) - 1)
        return min(self.retry_backoff_seconds[index], MAX_RETRY_DELAY_SECONDS)


# Existing callers and tests import the Laya-prefixed name.
HttpLayaClient = HttpSystemOneClient


def systemone_url(base_url: str) -> str:
    """The System One endpoint for either base-URL convention.

    The Laya sidecar is configured as a host root (``http://127.0.0.1:8110``),
    while cloud gateways follow the OpenAI convention and hand out a base that
    already ends in ``/v1``. Accepting both keeps the config honest instead of
    forcing one deployment to carry a URL that looks wrong.
    """

    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return join_url(base, "systemone")
    return join_url(base, "v1/systemone")


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "HttpLayaClient",
    "HttpSystemOneClient",
    "LayaClient",
    "LayaError",
    "SystemOneClient",
    "SystemOneError",
    "systemone_url",
]
