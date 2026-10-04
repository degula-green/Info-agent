"""Tavily clients: request shape, response shape, and the guard we keep.

Tavily moves both search and fetching out of our network. The vendor's own
protocol is pinned here, plus the two things that only matter because it is a
third party: a per-URL failure arriving inside an HTTP 200, and the
public-address check that no longer protects anything unless we run it.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from app.container import build_renderer, build_search_provider
from app.infrastructure.search.client import SearchError, SearchUnavailable
from app.infrastructure.search.tavily import TavilySearchProvider
from app.infrastructure.web.content_reader import ContentReader
from app.infrastructure.web.crawl4ai_client import (
    RenderError,
    RenderUnavailable,
    RenderedPage,
)
from app.infrastructure.web.tavily_extractor import TavilyRenderer
from app.testing.fake_providers import FakePageFetcher
from tests.support import make_settings


class StubResponse:
    """Enough of an http.client response for the clients to consume."""

    def __init__(self, body: bytes | str, status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self, *_args) -> bytes:
        return self._body

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def renderer(cls: type, **kwargs) -> TavilyRenderer:
    """A renderer with the pre-flight guard satisfied.

    The guard resolves DNS for real, and this development machine's proxy
    answers every public name with a 198.18.x address; the operator setting that
    allows that is part of the deployment, so the parsing tests use it too. The
    guard itself is asserted separately, with the setting off.
    """

    kwargs.setdefault("allow_private_addresses", True)
    return cls("https://api.tavily.com", **kwargs)


# --- search ----------------------------------------------------------------


def test_search_parses_results_and_names_the_provider() -> None:
    captured: dict = {}
    payload = {
        "query": "回调",
        "results": [
            {
                "url": "https://a.example.com/1",
                "title": "A",
                "content": "片段 A",
                "raw_content": "完整正文 A",
            },
            {"url": "https://b.example.com/2", "title": "B", "content": "片段 B"},
            {"url": "https://a.example.com/1", "title": "dup", "content": "重复"},
        ],
    }

    class Provider(TavilySearchProvider):
        def _open(self, request):
            captured["url"] = request.full_url
            headers = {key.lower(): value for key, value in dict(request.headers).items()}
            captured["auth"] = headers.get("authorization")
            captured["body"] = json.loads((request.data or b"{}").decode("utf-8"))
            return StubResponse(json.dumps(payload))

    results = Provider("https://api.tavily.com", api_key="tvly-x").search(
        "回调", top_k=5, include_domains=["open.feishu.cn"], language="zh-CN"
    )

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["auth"] == "Bearer tvly-x"
    assert captured["body"]["search_depth"] == "basic"
    assert captured["body"]["include_raw_content"] is False
    # Tavily takes the domain restriction natively; no site: surgery needed.
    assert captured["body"]["include_domains"] == ["open.feishu.cn"]
    assert [(item.url, item.rank, item.provider) for item in results] == [
        ("https://a.example.com/1", 1, "tavily"),
        ("https://b.example.com/2", 2, "tavily"),
    ]
    assert results[0].raw_content == "完整正文 A"


def test_search_without_a_key_uses_the_documented_keyless_header() -> None:
    captured: dict = {}

    class Provider(TavilySearchProvider):
        def _open(self, request):
            headers = {key.lower(): value for key, value in dict(request.headers).items()}
            captured["mode"] = headers.get("x-tavily-access-mode")
            captured["auth"] = headers.get("authorization")
            return StubResponse('{"results": []}')

    Provider("https://api.tavily.com").search(
        "回调", top_k=3, include_domains=[], language=""
    )

    assert captured["mode"] == "keyless"
    assert captured["auth"] is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, SearchUnavailable), (503, SearchUnavailable), (401, SearchError)],
)
def test_search_status_decides_whether_a_retry_can_help(status, expected) -> None:
    class Provider(TavilySearchProvider):
        def _open(self, request):
            raise urllib.error.HTTPError(request.full_url, status, "boom", {}, None)

    with pytest.raises(expected):
        Provider("https://api.tavily.com", api_key="k").search(
            "回调", top_k=3, include_domains=[], language=""
        )


def test_search_rejects_a_non_json_answer() -> None:
    class Provider(TavilySearchProvider):
        def _open(self, request):
            return StubResponse("<html>nope</html>")

    with pytest.raises(SearchError):
        Provider("https://api.tavily.com", api_key="k").search(
            "回调", top_k=3, include_domains=[], language=""
        )


# --- extraction ------------------------------------------------------------


def test_extract_returns_markdown_tagged_as_tavily() -> None:
    captured: dict = {}
    payload = {
        "results": [{"url": "https://a.example.com/1", "raw_content": "# 正文\n内容"}],
        "failed_results": [],
    }

    class Renderer(TavilyRenderer):
        def _open(self, request):
            captured["url"] = request.full_url
            captured["body"] = json.loads((request.data or b"{}").decode("utf-8"))
            return StubResponse(json.dumps(payload))

    page = renderer(Renderer, api_key="tvly-x").render("https://a.example.com/1")

    assert captured["url"] == "https://api.tavily.com/extract"
    assert captured["body"] == {
        "urls": ["https://a.example.com/1"],
        "extract_depth": "basic",
    }
    assert page.markdown.startswith("# 正文")
    # Provenance matters: this body did not come from our own fetch.
    assert page.fetch_method == "tavily"


def test_a_per_url_failure_inside_http_200_is_still_a_failure() -> None:
    """The vendor reports per-URL errors in the body, not in the status code."""

    payload = {
        "results": [],
        "failed_results": [{"url": "https://a.example.com/1", "error": "timeout"}],
    }

    class Renderer(TavilyRenderer):
        def _open(self, request):
            return StubResponse(json.dumps(payload))

    with pytest.raises(RenderUnavailable) as excinfo:
        renderer(Renderer, api_key="k").render("https://a.example.com/1")

    assert "timeout" in str(excinfo.value)


def test_an_empty_extraction_is_an_error_not_empty_evidence() -> None:
    class Renderer(TavilyRenderer):
        def _open(self, request):
            return StubResponse('{"results": []}')

    with pytest.raises(RenderError) as excinfo:
        renderer(Renderer, api_key="k").render("https://a.example.com/1")

    assert "no content" in str(excinfo.value)


def test_an_internal_address_never_reaches_the_vendor() -> None:
    """Self-hosting made this impossible; a hosted extractor needs the guard."""

    calls: list[str] = []

    class Renderer(TavilyRenderer):
        def _open(self, request):  # pragma: no cover - must not be reached
            calls.append(request.full_url)
            raise AssertionError("must not call Tavily for an internal address")

    renderer = Renderer(
        "https://api.tavily.com", api_key="k", allow_private_addresses=False
    )

    with pytest.raises(RenderError):
        renderer.render("http://127.0.0.1:8080/admin")

    assert calls == []


def test_the_reader_records_which_renderer_produced_the_body() -> None:
    class FakeRenderer:
        def render(self, url: str) -> RenderedPage:
            return RenderedPage(
                url=url,
                final_url=url,
                status=200,
                markdown="正文",
                fetch_method="tavily",
            )

    reader = ContentReader(
        FakePageFetcher({"https://a.example.com/1": {"content": "<p>短</p>"}}),
        renderer=FakeRenderer(),
        min_text_chars=500,
    )

    page = reader.read("https://a.example.com/1")

    assert page.fetch_method == "tavily"


# --- configuration ---------------------------------------------------------


def test_configuration_picks_the_provider_and_the_renderer() -> None:
    tavily = make_settings(
        web_search_provider="tavily",
        web_renderer="tavily",
        tavily_api_key="tvly-x",
        web_allow_private_addresses=True,
    )

    provider = build_search_provider(tavily)
    renderer = build_renderer(tavily)

    assert isinstance(provider, TavilySearchProvider)
    assert isinstance(renderer, TavilyRenderer)
    assert renderer.api_key == "tvly-x"
    assert renderer.allow_private_addresses is True


def test_configuration_can_turn_both_off() -> None:
    settings = make_settings(web_search_provider="none", web_renderer="none")

    assert build_search_provider(settings) is None
    assert build_renderer(settings) is None


def test_the_self_hosted_path_is_still_one_setting_away() -> None:
    from app.infrastructure.search.searxng import SearxngSearchProvider
    from app.infrastructure.web.crawl4ai_client import Crawl4AIClient

    settings = make_settings(
        web_search_provider="searxng",
        web_renderer="crawl4ai",
        searxng_base_url="http://127.0.0.1:18080",
        crawl4ai_base_url="http://127.0.0.1:11235",
    )

    assert isinstance(build_search_provider(settings), SearxngSearchProvider)
    assert isinstance(build_renderer(settings), Crawl4AIClient)


def test_an_unknown_provider_name_is_refused_loudly() -> None:
    with pytest.raises(RuntimeError):
        build_search_provider(make_settings(web_search_provider="bing"))
