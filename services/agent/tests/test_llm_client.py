"""HTTP adapter tests for the optional OpenAI-compatible clients."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from app.infrastructure.http import HttpResult, IntegrationError
from app.infrastructure.llm.client import (
    LLMError,
    LLMStreamCancelled,
    LLMUnavailable,
    OpenAIChatClient,
)
from app.infrastructure.llm.stream_http import StreamCancelled


class RecordingHttp:
    def __init__(
        self, body: dict[str, Any] | None = None, *, errors: list[Exception] | None = None
    ) -> None:
        self.body = body or {
            "choices": [{"message": {"content": "{\"ok\":true}"}}]
        }
        self.calls: list[dict[str, Any]] = []
        self.errors = list(errors or [])

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
        if self.errors:
            raise self.errors.pop(0)
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


def _http_error(status: int, body: dict[str, Any]) -> Exception:
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://api.example/v1/chat/completions",
        status,
        "Bad Request",
        {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


def test_a_nested_provider_error_code_is_surfaced() -> None:
    """DashScope reports arrears as {"error": {"code": "Arrearage"}}.

    The body itself is never carried forward, but the code is: "(400 Arrearage)"
    tells an operator to top up an account, a bare "(400)" does not.
    """

    from app.infrastructure.http import _error_code

    exc = _http_error(
        400,
        {
            "error": {
                "message": "Access denied, please make sure your account is in good standing",
                "type": "Arrearage",
                "code": "Arrearage",
            }
        },
    )

    assert _error_code(exc) == "Arrearage"


def test_the_provider_error_code_reaches_the_message_without_the_body(monkeypatch) -> None:
    import urllib.request

    from app.infrastructure.http import HttpClient

    error = _http_error(
        400,
        {
            "error": {
                "message": "Access denied, please make sure your account is in good standing",
                "code": "Arrearage",
            }
        },
    )
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error)
    )

    with pytest.raises(IntegrationError) as excinfo:
        HttpClient().request("POST", "https://api.example/v1/chat/completions", body={})

    message = str(excinfo.value)
    assert "400 Arrearage" in message
    # The provider's prose (which may carry account detail) stays out of it.
    assert "good standing" not in message


def test_openai_client_rejects_an_empty_choice_list() -> None:
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        http=RecordingHttp({"choices": []}),
    )

    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "hello"}])


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"objective": {"type": "string"}},
    "required": ["objective"],
    "additionalProperties": False,
}


def test_a_structured_call_sends_the_strict_json_schema() -> None:
    http = RecordingHttp()
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        response_format="json_schema",
        http=http,
    )

    content = client.complete_structured(
        [{"role": "user", "content": "hello"}],
        schema=PLAN_SCHEMA,
        name="agent_plan",
    )

    assert content == '{"ok":true}'
    assert http.calls[0]["body"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "agent_plan", "strict": True, "schema": PLAN_SCHEMA},
    }
    assert client.last_call_count == 1


def test_a_provider_that_refuses_the_schema_falls_back_to_json_object() -> None:
    """qwen-compatible gateways reject unknown response_format values with 400."""

    http = RecordingHttp(
        errors=[
            IntegrationError(
                "remote HTTP request failed (400)",
                status=400,
                retryable=False,
                error_code="invalid_parameter",
            )
        ]
    )
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        response_format="json_schema",
        http=http,
    )

    content = client.complete_structured(
        [{"role": "user", "content": "hello"}],
        schema=PLAN_SCHEMA,
        name="agent_plan",
    )

    assert content == '{"ok":true}'
    assert http.calls[0]["body"]["response_format"]["type"] == "json_schema"
    assert http.calls[1]["body"]["response_format"] == {"type": "json_object"}
    # Both requests reached the provider, so both count against the Task.
    assert client.last_call_count == 2

    # The rejection is remembered: the next call skips the doomed attempt.
    client.complete_structured(
        [{"role": "user", "content": "again"}], schema=PLAN_SCHEMA
    )
    assert http.calls[2]["body"]["response_format"] == {"type": "json_object"}


def test_a_server_error_is_not_mistaken_for_a_schema_rejection() -> None:
    http = RecordingHttp(
        errors=[
            IntegrationError(
                "remote HTTP request failed (503)",
                status=503,
                retryable=True,
            )
        ]
    )
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        response_format="json_schema",
        http=http,
    )

    with pytest.raises(LLMError):
        client.complete_structured(
            [{"role": "user", "content": "hello"}], schema=PLAN_SCHEMA
        )
    assert len(http.calls) == 1


def test_a_client_that_never_asked_for_a_schema_still_sends_json_object() -> None:
    """The understanding and answer clients keep their old request shape."""

    http = RecordingHttp()
    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        response_format="json_object",
        http=http,
    )

    client.complete_structured(
        [{"role": "user", "content": "hello"}], schema=PLAN_SCHEMA
    )

    assert http.calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_stream_complete_parses_sse_content_deltas() -> None:
    captured: dict[str, Any] = {}

    def fake_transport(method: str, url: str, **kwargs: Any):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return iter(
            [
                'data: {"choices":[{"delta":{"content":"{\\""}}]}\n',
                "\n",
                'data: {"choices":[{"delta":{"content":"answer"}}]}\n',
                "data: [DONE]\n",
            ]
        )

    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        api_key="secret",
        model="qwen-test",
        http=RecordingHttp(),
        stream_transport=fake_transport,
    )

    chunks = list(client.stream_complete([{"role": "user", "content": "hello"}]))

    assert chunks == ['{"', "answer"]
    assert captured["method"] == "POST"
    assert captured["url"] == "http://model.local/v1/chat/completions"
    assert captured["token"] == "secret"
    assert captured["body"]["stream"] is True
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert client.last_call_count == 1


def test_stream_complete_maps_cancellation_to_a_dedicated_error() -> None:
    def fake_transport(method: str, url: str, **kwargs: Any):
        def generate():
            raise StreamCancelled("stopped")
            yield ""  # pragma: no cover - makes this a generator

        return generate()

    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        http=RecordingHttp(),
        stream_transport=fake_transport,
    )

    with pytest.raises(LLMStreamCancelled):
        list(client.stream_complete([{"role": "user", "content": "hello"}]))


def test_stream_complete_maps_retryable_transport_errors() -> None:
    def fake_transport(method: str, url: str, **kwargs: Any):
        raise IntegrationError("remote stream idle timeout", retryable=True)

    client = OpenAIChatClient(
        base_url="http://model.local/v1",
        model="qwen-test",
        http=RecordingHttp(),
        stream_transport=fake_transport,
    )

    with pytest.raises(LLMUnavailable):
        list(client.stream_complete([{"role": "user", "content": "hello"}]))
