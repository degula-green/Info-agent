"""Entity extraction client: window extraction and L4 disambiguation.

Two properties of this model shape the code below:

1. It is a reasoning model, and reasoning tokens count against ``max_tokens``.
   When the budget runs out the API answers HTTP 200 with an *empty* body
   rather than an error. An empty completion must therefore be treated as a
   failed call, never as "this window mentions no entities" - doing the latter
   would silently drop every extracted fact.
2. Disabling thinking removes the reasoning tokens entirely. Measured
   2026-10-09 on real 20-message windows against deepseek-flash, that took a
   call from ~3.4s to ~1.7s and roughly halved the token count while producing
   equal or better extractions, so it is the default.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.config import settings
from app.infrastructure.http import HttpClient, IntegrationError, join_url


class ExtractionError(RuntimeError):
    """No usable JSON came back; the caller retries or degrades."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class EntityExtractionClient:
    """OpenAI-compatible chat/completions call that must return a JSON object."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        disable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        http: HttpClient | None = None,
    ) -> None:
        resolved_url = base_url if base_url is not None else settings.extract_base_url
        self.base_url = (resolved_url or "").rstrip("/")
        self.api_key = api_key if api_key is not None else settings.extract_api_key
        self.model = str(model or settings.extract_model or "deepseek-flash")
        self.disable_thinking = (
            settings.extract_disable_thinking
            if disable_thinking is None
            else bool(disable_thinking)
        )
        resolved_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else settings.extract_reasoning_effort
        )
        self.reasoning_effort = str(resolved_effort or "").strip()
        resolved_tokens = (
            max_tokens if max_tokens is not None else settings.extract_max_tokens
        )
        self.max_tokens = max(int(resolved_tokens), 1)
        resolved_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.extract_timeout_seconds
        )
        self.timeout_seconds = max(float(resolved_timeout), 1.0)
        self.http = http or HttpClient()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def extract(self, prompt: str) -> dict[str, Any]:
        parsed, _ = self.extract_with_usage(prompt)
        return parsed

    def extract_with_usage(self, prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Same as :meth:`extract` but also returns the provider's usage block.

        Kept separate so the hot path stays unchanged; the measurement script
        needs the token counts, and re-deriving them from the response elsewhere
        would duplicate this parsing.
        """
        if not self.base_url:
            raise ExtractionError("extraction base URL is not configured")
        try:
            result = self.http.request(
                "POST",
                join_url(self.base_url, "chat/completions"),
                body=self._payload(prompt),
                timeout=self.timeout_seconds,
                token=self.api_key or None,
            )
        except IntegrationError as exc:
            raise ExtractionError(
                "extraction request failed", retryable=exc.retryable
            ) from exc
        try:
            body = result.json()
        except IntegrationError as exc:
            raise ExtractionError("extraction response is not valid JSON") from exc
        content = _completion_text(body)
        if not content:
            # HTTP 200 with no content means reasoning consumed the token
            # budget. Retry instead of reporting "no entities found".
            raise ExtractionError(
                "extraction returned an empty completion", retryable=True
            )
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise ExtractionError(
                "extraction completion is not valid JSON", retryable=True
            ) from exc
        if not isinstance(parsed, dict):
            raise ExtractionError("extraction completion is not a JSON object")
        usage = body.get("usage") if isinstance(body, dict) else None
        return parsed, usage if isinstance(usage, dict) else {}

    def _payload(self, prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        elif self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload


def _completion_text(body: Any) -> str:
    if not isinstance(body, Mapping):
        raise ExtractionError("extraction response is not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ExtractionError("extraction response has no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ExtractionError("extraction response choice is not an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ExtractionError("extraction response has no message")
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""
