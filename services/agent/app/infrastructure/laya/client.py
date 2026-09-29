"""HTTP client for the Laya multilingual decision sidecar."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.infrastructure.http import HttpClient, IntegrationError, join_url


class LayaError(RuntimeError):
    classification = "permanent_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.classification = "retryable_error" if retryable else "permanent_error"


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


class HttpLayaClient:
    """Calls the TypeSafe-compatible endpoint exposed by ``laya-serve``."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "multilingual",
        timeout_seconds: float = 5.0,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.http = http or HttpClient()

    def predict(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        max_len: int | None = None,
        head_max_len: int | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise LayaError("Laya base URL is required", retryable=True)

        payload: dict[str, Any] = {
            "state": state,
            "questions": questions,
            "model": self.model,
        }
        if max_len is not None:
            payload["max_len"] = max_len
        if head_max_len is not None:
            payload["head_max_len"] = head_max_len

        try:
            response = self.http.request(
                "POST",
                join_url(self.base_url, "v1/systemone"),
                body=payload,
                token=self.api_key or None,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            raise LayaError(str(exc), retryable=exc.retryable) from exc

        try:
            body = response.json()
        except IntegrationError as exc:
            raise LayaError("Laya response is not valid JSON") from exc
        if not isinstance(body, Mapping):
            raise LayaError("Laya response is not a JSON object")
        return dict(body)
