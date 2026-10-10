"""RAG client error classification.

A slow or briefly unavailable RAG service must not fail a Task outright: the
kernel only retries a step whose error is classified ``retryable_error``. A 4xx
describes the request itself, so it stays permanent.
"""

from __future__ import annotations

import pytest

from app.infrastructure.http import IntegrationError
from app.infrastructure.rag.client import HttpRAGClient, RAGUnavailable
from app.kernel.errors import classify_error


class _StubHttp:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def request(self, *_args, **_kwargs):
        raise self.error


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return type("Response", (), {"json": lambda self: {"items": []}})()


def _client(error: Exception) -> HttpRAGClient:
    return HttpRAGClient(
        base_url="http://127.0.0.1:8000",
        service_token="token",
        http=_StubHttp(error),
    )


def test_transient_rag_failure_is_retryable() -> None:
    client = _client(
        IntegrationError(
            "remote HTTP request failed (503)", status=503, retryable=True
        )
    )

    with pytest.raises(RAGUnavailable) as excinfo:
        client.search_sources(
            {}, user_id="u", organization_id="o", request_id="r", trace_id="t"
        )

    assert classify_error(excinfo.value) == "retryable_error"
    assert "503" in str(excinfo.value)


def test_request_level_rag_failure_stays_permanent() -> None:
    client = _client(
        IntegrationError(
            "remote HTTP request failed (401)", status=401, retryable=False
        )
    )

    with pytest.raises(RAGUnavailable) as excinfo:
        client.search_content(
            {}, user_id="u", organization_id="o", request_id="r", trace_id="t"
        )

    assert classify_error(excinfo.value) == "permanent_error"


def test_tree_search_uses_the_tree_endpoint() -> None:
    http = _RecordingHttp()
    client = HttpRAGClient(
        base_url="http://127.0.0.1:8000",
        service_token="token",
        http=http,
    )

    client.search_tree(
        {"query": "青云飞鹏"},
        user_id="user-1",
        organization_id="org-1",
        request_id="request-1",
        trace_id="trace-1",
    )

    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url.endswith("/api/v1/search/tree")
    assert kwargs["headers"]["X-User-ID"] == "user-1"
    assert kwargs["headers"]["X-Organization-ID"] == "org-1"
