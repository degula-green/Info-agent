"""Minimal OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any, Mapping

from app.infrastructure.http import HttpClient, IntegrationError, join_url

logger = logging.getLogger("agent.llm")


# A provider that cannot decode the requested JSON Schema answers the request
# itself with a client error. These are the statuses that mean "this provider
# does not know this response_format", not "your request was malformed".
_SCHEMA_REJECTION_STATUSES = frozenset({400, 404, 415, 422})


class LLMError(RuntimeError):
    classification = "permanent_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LLMUnavailable(LLMError):
    classification = "retryable_error"


class OpenAIChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str,
        timeout_seconds: float = 10.0,
        max_output_tokens: int = 300,
        response_format: str = "json_object",
        json_schema_fallback: bool = True,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.response_format = response_format
        self.json_schema_fallback = json_schema_fallback
        self.http = http or HttpClient()
        # Concurrent read-only Steps may share this client. Each worker thread
        # gets its own context, so call accounting belongs there.
        self._call_count: ContextVar[int] = ContextVar(
            f"agent_llm_calls_{id(self)}", default=0
        )
        # Remembered after the first rejection so every later call goes
        # straight to the supported mode instead of paying for the failure.
        self._json_schema_disabled = False

    @property
    def last_call_count(self) -> int:
        return max(int(self._call_count.get()), 0)

    @last_call_count.setter
    def last_call_count(self, value: int) -> None:
        self._call_count.set(max(int(value), 0))

    def complete(self, messages: list[dict[str, str]]) -> str:
        return self._complete(messages, response_format=self._plain_response_format())

    def complete_structured(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        name: str = "structured_output",
    ) -> str:
        """Ask for one strict JSON Schema answer, degrading to json_object.

        Strict decoding is what keeps a required argument (``document_ref``)
        from being dropped between two steps. Providers that do not implement
        it reject the request instead of the content, so the same messages are
        re-sent with the widest request they do understand: the Planner still
        validates the answer against its own models either way.
        """

        plain = self._plain_response_format()
        attempted_schema = False
        if (
            plain is not None
            and not self._json_schema_disabled
            and str(self.response_format or "").strip().lower()
            in {"json_schema", "schema", "strict"}
        ):
            attempted_schema = True
            payload = {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            }
            try:
                return self._complete(messages, response_format=payload)
            except LLMError as exc:
                if not self.json_schema_fallback or not _is_schema_rejection(exc):
                    raise
                logger.warning(
                    "provider rejected response_format=json_schema for %s (%s); "
                    "falling back to json_object",
                    name,
                    getattr(exc, "error_code", None) or getattr(exc, "status", None),
                )
                self._json_schema_disabled = True
        content = self.complete(messages)
        if attempted_schema:
            # The rejected request was a real call too; the Task budget counts
            # what the provider was asked, not what succeeded.
            self.last_call_count += 1
        return content

    def _plain_response_format(self) -> dict[str, str] | None:
        value = str(self.response_format or "").strip().lower()
        if not value or value in {"none", "json_schema", "schema", "strict"}:
            # json_schema is only meaningful per call, and "none" means no
            # response_format at all; the plain call sends neither.
            return None if value in {"", "none"} else {"type": "json_object"}
        return {"type": value}

    def _complete(
        self, messages: list[dict[str, str]], *, response_format: dict[str, str] | None
    ) -> str:
        if not self.base_url or not self.model:
            raise LLMUnavailable("LLM base URL and model are required")
        self.last_call_count = 1
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": self.max_output_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        url = (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else join_url(self.base_url, "chat/completions")
        )
        try:
            result = self.http.request(
                "POST",
                url,
                body=payload,
                token=self.api_key or None,
                timeout=self.timeout_seconds,
            )
        except IntegrationError as exc:
            error_type = LLMUnavailable if exc.retryable else LLMError
            error = error_type(str(exc))
            # The caller decides whether a rejected response_format is worth a
            # second attempt; the status and provider code are what it reads.
            error.status = exc.status  # type: ignore[attr-defined]
            error.error_code = exc.error_code  # type: ignore[attr-defined]
            raise error from exc
        try:
            body = result.json()
        except IntegrationError as exc:
            raise LLMError("LLM response is not valid JSON") from exc
        return _extract_content(body)


def _is_schema_rejection(exc: LLMError) -> bool:
    """True when the provider refused the request because of the schema."""

    if exc.classification != "permanent_error":
        return False
    status = getattr(exc, "status", None)
    if status in _SCHEMA_REJECTION_STATUSES:
        return True
    code = str(getattr(exc, "error_code", "") or "").lower()
    return any(
        hint in code
        for hint in ("response_format", "json_schema", "json_schema_error", "unsupported")
    )


def _extract_content(body: Any) -> str:
    if not isinstance(body, Mapping):
        raise LLMError("LLM response is not a JSON object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM response has no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise LLMError("LLM choice is not an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LLMError("LLM response has no message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
    raise LLMError("LLM message content is empty")


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("LLM output must be a JSON object")
    return value
