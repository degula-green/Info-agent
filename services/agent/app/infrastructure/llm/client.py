"""Minimal OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.infrastructure.http import HttpClient, IntegrationError, join_url


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
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.response_format = response_format
        self.http = http or HttpClient()
        self.last_call_count = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        if not self.base_url or not self.model:
            raise LLMUnavailable("LLM base URL and model are required")
        self.last_call_count = 1
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": self.max_output_tokens,
        }
        if self.response_format and self.response_format.lower() != "none":
            payload["response_format"] = {"type": self.response_format}
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
            raise error_type(str(exc)) from exc
        try:
            body = result.json()
        except IntegrationError as exc:
            raise LLMError("LLM response is not valid JSON") from exc
        return _extract_content(body)


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
