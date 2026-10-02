"""Step-4 capabilities: fetch guard rails, extraction, citation filtering."""

from __future__ import annotations

import urllib.error

import pytest
from pydantic import ValidationError

from app.capabilities.answer import (
    AnswerComposeCapability,
    AnswerComposeInput,
    AnswerComposeResult,
)
from app.capabilities.todo import (
    TodoCreateCapability,
    TodoCreateInput,
    TodoCreateResult,
)
from app.capabilities.web import (
    WebExtractCapability,
    WebExtractInput,
    WebExtractResult,
    WebFetchCapability,
    WebFetchInput,
    WebFetchResult,
)
from app.providers.page_fetcher import (
    HttpPageFetcher,
    PageFetchError,
    PageFetchRetryable,
    _ValidatingRedirectHandler,
    assert_public_url,
)
from app.testing.fake_providers import FakeAnswerProvider, FakePageFetcher
from app.testing.in_memory_todo_store import InMemoryTodoStore

PUBLIC_URL = "https://93.184.216.34/page"


class _StubResponse:
    """Enough of an http.client response for the fetcher to consume."""

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        url: str = PUBLIC_URL,
        status: int = 200,
    ) -> None:
        self._body = body
        self._offset = 0
        self.headers = {"Content-Type": content_type}
        self.status = status
        self._url = url

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body[self._offset:]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubFetcher(HttpPageFetcher):
    """Replaces the socket with a canned response."""

    def __init__(self, response: _StubResponse, **kwargs) -> None:
        super().__init__(**kwargs)
        self._response = response

    def _open(self, request):
        return self._response


def test_descriptor_schemas_come_from_the_capability_models() -> None:
    fetch = WebFetchCapability(FakePageFetcher())
    extract = WebExtractCapability()
    compose = AnswerComposeCapability(FakeAnswerProvider())

    assert fetch.descriptor.input_schema == WebFetchInput.model_json_schema()
    assert fetch.descriptor.output_schema == WebFetchResult.model_json_schema()
    assert extract.descriptor.input_schema == WebExtractInput.model_json_schema()
    assert extract.descriptor.output_schema == WebExtractResult.model_json_schema()
    assert compose.descriptor.input_schema == AnswerComposeInput.model_json_schema()
    assert compose.descriptor.output_schema == AnswerComposeResult.model_json_schema()


def test_the_existing_todo_capability_carries_schemas_too() -> None:
    descriptor = TodoCreateCapability(InMemoryTodoStore()).descriptor
    assert descriptor.input_schema == TodoCreateInput.model_json_schema()
    assert descriptor.output_schema == TodoCreateResult.model_json_schema()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "https://",
    ],
)
def test_only_http_and_https_urls_are_fetchable(url: str) -> None:
    with pytest.raises(PageFetchError):
        assert_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.5/admin",
        "http://192.168.1.10/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
    ],
)
def test_private_and_loopback_addresses_are_refused(url: str) -> None:
    with pytest.raises(PageFetchError) as excinfo:
        assert_public_url(url)
    assert "non-public" in str(excinfo.value)


def test_a_public_literal_address_is_allowed() -> None:
    assert assert_public_url(PUBLIC_URL) == PUBLIC_URL


def test_credentials_in_the_url_are_refused() -> None:
    with pytest.raises(PageFetchError):
        assert_public_url("https://user:secret@93.184.216.34/x")


def test_every_redirect_hop_is_validated() -> None:
    handler = _ValidatingRedirectHandler(3)
    with pytest.raises(PageFetchError):
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://127.0.0.1/internal"
        )


def test_private_addresses_can_be_allowed_explicitly() -> None:
    """Fake-IP proxies resolve every host to a non-public address."""

    assert (
        assert_public_url("http://127.0.0.1/x", allow_private_addresses=True)
        == "http://127.0.0.1/x"
    )

    fetcher = _StubFetcher(_StubResponse(b"<p>ok</p>"), allow_private_addresses=True)
    assert fetcher.fetch("http://127.0.0.1/x").content == "<p>ok</p>"


def test_the_private_address_switch_still_rejects_other_schemes() -> None:
    with pytest.raises(PageFetchError):
        assert_public_url("file:///etc/passwd", allow_private_addresses=True)


def test_fetch_reads_the_page_and_reports_truncation() -> None:
    fetcher = _StubFetcher(
        _StubResponse(b"<html><body>hello</body></html>"), max_bytes=16
    )

    document = fetcher.fetch(PUBLIC_URL)

    assert document.content == "<html><body>hell"
    assert document.truncated is True
    assert document.content_type == "text/html"
    assert document.final_url == PUBLIC_URL


def test_fetch_rejects_a_non_text_content_type() -> None:
    fetcher = _StubFetcher(_StubResponse(b"%PDF-1.4", content_type="application/pdf"))

    with pytest.raises(PageFetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert "content type" in str(excinfo.value)


def test_fetch_accepts_the_markdown_twin_of_a_page() -> None:
    """Docs sites publish .md next to the HTML and say it is better for AI."""

    fetcher = _StubFetcher(
        _StubResponse(
            "# 智能助手\n\n自然语言生成可执行代码。".encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
    )

    document = fetcher.fetch(PUBLIC_URL)

    assert document.content_type == "text/markdown"
    assert document.content.startswith("# 智能助手")


def test_extract_reads_markdown_as_text() -> None:
    capability = WebExtractCapability()
    markdown = "# 标题\n\n第一段正文。\n\n- 要点"

    result = capability.execute(
        capability.validate({"document": markdown, "url": PUBLIC_URL})
    )

    assert "第一段正文。" in result["text"]
    assert result["evidence"][0]["url"] == PUBLIC_URL


def test_a_gzipped_response_is_decoded_instead_of_read_as_noise() -> None:
    """CDNs gzip even when identity is requested, and urllib does not decode."""

    import gzip

    html = "<html><head><title>压缩页</title></head><body><p>正文内容</p></body></html>"
    response = _StubResponse(gzip.compress(html.encode("utf-8")))
    response.headers["Content-Encoding"] = "gzip"

    document = _StubFetcher(response).fetch(PUBLIC_URL)

    assert document.content.startswith("<html>")
    assert "正文内容" in document.content
    assert document.truncated is False


def test_the_fetcher_asks_for_an_uncompressed_body() -> None:
    captured: dict = {}

    class _RecordingFetcher(HttpPageFetcher):
        def _open(self, request):
            captured.update(dict(request.headers))
            return _StubResponse(b"<p>ok</p>")

    _RecordingFetcher().fetch(PUBLIC_URL)

    assert captured.get("Accept-encoding") == "identity"


def test_a_url_the_request_line_cannot_carry_is_a_clean_error() -> None:
    """A raw UnicodeEncodeError used to escape as a capability failure."""

    class _NonAsciiFetcher(HttpPageFetcher):
        def _open(self, request):
            raise UnicodeEncodeError("ascii", "GET /网址内容 HTTP/1.1", 5, 9, "nope")

    with pytest.raises(PageFetchError) as excinfo:
        _NonAsciiFetcher().fetch(PUBLIC_URL)

    assert excinfo.value.code == "invalid_url"
    assert type(excinfo.value) is PageFetchError


@pytest.mark.parametrize(
    "status, expected",
    [
        (429, PageFetchRetryable),
        (503, PageFetchRetryable),
        (404, PageFetchError),
    ],
)
def test_http_status_decides_whether_a_retry_can_help(status, expected) -> None:
    class _FailingFetcher(HttpPageFetcher):
        def _open(self, request):
            raise urllib.error.HTTPError(PUBLIC_URL, status, "boom", {}, None)

    with pytest.raises(expected) as excinfo:
        _FailingFetcher().fetch(PUBLIC_URL)

    assert type(excinfo.value) is expected


def test_extract_pulls_title_text_links_and_evidence() -> None:
    capability = WebExtractCapability()
    html = (
        "<html><head><title>示例页面</title>"
        "<style>body{color:red}</style></head>"
        "<body><p>第一段正文</p><script>alert(1)</script>"
        "<a href='/next'>下一页</a></body></html>"
    )

    result = capability.execute(
        capability.validate({"document": html, "url": PUBLIC_URL})
    )

    assert result["title"] == "示例页面"
    assert "第一段正文" in result["text"]
    assert "alert(1)" not in result["text"]
    assert "color:red" not in result["text"]
    assert result["links"] == ["https://93.184.216.34/next"]
    evidence = result["evidence"][0]
    assert evidence["evidence_id"].startswith("ev-")
    assert evidence["url"] == PUBLIC_URL
    assert "第一段正文" in evidence["snippet"]


def test_extract_accepts_plain_text() -> None:
    capability = WebExtractCapability()
    result = capability.execute(capability.validate({"document": "只是一段纯文本"}))

    assert result["text"] == "只是一段纯文本"
    assert result["evidence"][0]["source"] == "document"


def test_the_same_document_yields_the_same_evidence_id() -> None:
    """A retry must not look like a second, different source."""

    capability = WebExtractCapability()
    payload = {"document": "<p>同样内容</p>", "url": PUBLIC_URL}
    first = capability.execute(capability.validate(payload))
    second = capability.execute(capability.validate(payload))

    assert (
        first["evidence"][0]["evidence_id"]
        == second["evidence"][0]["evidence_id"]
    )


def test_answer_compose_drops_citations_it_cannot_trace() -> None:
    provider = FakeAnswerProvider(
        citations=[
            {"evidence_id": "ev-known", "quote": "有出处"},
            {"evidence_id": "ev-invented", "quote": "编的"},
        ]
    )
    capability = AnswerComposeCapability(provider)

    result = capability.execute(
        capability.validate(
            {"question": "问一句", "evidence": [{"evidence_id": "ev-known"}]}
        )
    )

    assert [item["evidence_id"] for item in result["citations"]] == ["ev-known"]


def test_answer_compose_reports_its_model_spend() -> None:
    capability = AnswerComposeCapability(FakeAnswerProvider(model_calls=2))
    result = capability.execute(
        capability.validate({"question": "问一句", "evidence": []})
    )
    assert result["model_calls"] == 2


def test_unknown_arguments_are_rejected() -> None:
    capability = WebFetchCapability(FakePageFetcher())
    with pytest.raises(ValidationError):
        capability.validate({"url": PUBLIC_URL, "time": "明天"})
