"""HTTP adapter tests for the optional OpenAI-compatible clients."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from app.infrastructure.http import HttpResult
from app.infrastructure.llm.client import LLMError, OpenAIChatClient


class RecordingHttp:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or {
            "choices": [{"message": {"content": "{\"ok\":true}"}}]
        }
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> HttpResult:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "headers": dict(headers or {}),
                "timeout": timeout,
                "token": token,
            }
        )
        return HttpResult(
            status=200,
            headers={},
            body=json.dumps(self.body).encode("utf-8"),
        )


def test_openai_client_builds_json_chat_request() -> None:
    http = RecordingHttp()
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        api_key="secret",
        model="qwen-test",
        timeout_seconds=7,
        max_output_tokens=123,
        http=http,
    )

    content = client.complete(
        [{"role": "user", "content": "hello"}]
    )

    assert content == '{"ok":true}'
    assert http.calls[0]["url"] == "http://model.local/v1/chat/completions"
    assert http.calls[0]["token"] == "secret"
    assert http.calls[0]["body"]["model"] == "qwen-test"
    assert http.calls[0]["body"]["max_tokens"] == 123
    assert http.calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_openai_client_rejects_an_empty_choice_list() -> None:
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        http=RecordingHttp({"choices": []}),
    )

    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "hello"}])
