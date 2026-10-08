"""Entity extraction client: request shape and the empty-completion guard."""

import json

import pytest

from app.infrastructure.extraction.client import (
    EntityExtractionClient,
    ExtractionError,
)
from app.infrastructure.http import HttpResult


def _http_returning(payload):
    class FakeHttp:
        def __init__(self):
            self.payload = None

        def request(self, method, url, **kwargs):
            self.payload = kwargs.get("body")
            body = payload if isinstance(payload, str) else json.dumps(payload)
            return HttpResult(200, {}, body.encode("utf-8"))

    return FakeHttp()


def test_extract_disables_thinking_and_parses_the_json_object():
    http = _http_returning(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"entities": [{"name": "A项目", "type": "project"}]}
                        )
                    }
                }
            ]
        }
    )
    client = EntityExtractionClient(
        base_url="https://example.com/v1", api_key="k", http=http
    )

    result = client.extract("prompt")

    assert result["entities"][0]["name"] == "A项目"
    assert http.payload["thinking"] == {"type": "disabled"}
    assert http.payload["response_format"] == {"type": "json_object"}
    assert http.payload["temperature"] == 0
    assert "reasoning_effort" not in http.payload


def test_extract_uses_reasoning_effort_when_thinking_is_kept():
    http = _http_returning(
        {"choices": [{"message": {"content": "{}"}}]}
    )
    client = EntityExtractionClient(
        base_url="https://example.com/v1",
        http=http,
        disable_thinking=False,
        reasoning_effort="low",
    )

    client.extract("prompt")

    assert http.payload["reasoning_effort"] == "low"
    assert "thinking" not in http.payload


def test_extract_rejects_an_empty_completion_as_retryable():
    # A reasoning model that exhausts max_tokens answers 200 with no content.
    # Treating that as "no entities" would silently drop the whole window.
    http = _http_returning({"choices": [{"message": {"content": ""}}]})
    client = EntityExtractionClient(
        base_url="https://example.com/v1", http=http
    )

    with pytest.raises(ExtractionError) as excinfo:
        client.extract("prompt")

    assert excinfo.value.retryable is True


def test_extract_rejects_a_non_json_completion_as_retryable():
    http = _http_returning(
        {"choices": [{"message": {"content": "不是 JSON"}}]}
    )
    client = EntityExtractionClient(
        base_url="https://example.com/v1", http=http
    )

    with pytest.raises(ExtractionError) as excinfo:
        client.extract("prompt")

    assert excinfo.value.retryable is True


def test_extract_rejects_a_json_array():
    http = _http_returning({"choices": [{"message": {"content": "[1,2]"}}]})
    client = EntityExtractionClient(
        base_url="https://example.com/v1", http=http
    )

    with pytest.raises(ExtractionError):
        client.extract("prompt")


def test_extract_requires_a_base_url():
    client = EntityExtractionClient(base_url="")

    assert client.configured is False
    with pytest.raises(ExtractionError):
        client.extract("prompt")
