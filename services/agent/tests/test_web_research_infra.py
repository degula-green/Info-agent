"""Infrastructure behind web.research: URL trust, search and rendering.

Every one of these has a failure mode that quietly widens what the Agent may
reach -- an invented domain, a search engine returning HTML instead of JSON, a
renderer that answers for a page nobody found. They are pinned here because a
capability test would only notice them as an odd warning.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from app.infrastructure.search.client import SearchError, SearchUnavailable, merge_results
from app.infrastructure.search.searxng import SearxngSearchProvider
from app.infrastructure.web.content_reader import ContentReader, ContentUnavailable
from app.infrastructure.web.crawl4ai_client import (
    Crawl4AIClient,
    RenderError,
    RenderUnavailable,
)
from app.infrastructure.web.extractor import clip, normalize_text
from app.infrastructure.web.evidence import select_quote
from app.infrastructure.web.url_tools import (
    domains_in_text,
    extract_http_urls,
    known_domains,
    load_aliases,
    normalize_queries,
    normalize_url,
    resolve_platform_domains,
    trusted_urls,
    unique_urls,
    with_site_filter,
)
from app.testing.fake_providers import FakePageFetcher
from app.providers.page_fetcher import PageFetchRetryable


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


# --- URL handling ----------------------------------------------------------


def test_only_http_urls_are_extracted() -> None:
    text = "看 https://example.com/a，还有 http://93.184.216.34/b 和 ftp://nope"

    assert extract_http_urls(text) == [
        "https://example.com/a",
        "http://93.184.216.34/b",
    ]


def test_chinese_text_glued_to_a_link_is_not_part_of_the_url() -> None:
    """Observed in production: "搜索一下https://www.luogu.com.cn/网址内容是什么？".

    The loose class used to swallow the trailing sentence, and the HTTP client
    then died encoding the request line as ASCII.
    """

    text = "搜索一下飞书开放平台的主要内容是什么？以及搜索一下https://www.luogu.com.cn/网址内容是什么？"

    assert extract_http_urls(text) == ["https://www.luogu.com.cn/"]
    assert trusted_urls(["https://www.luogu.com.cn/"], text) == [
        "https://www.luogu.com.cn/"
    ]


def test_markdown_link_syntax_does_not_leak_into_the_url() -> None:
    text = "看 [https://www.luogu.com.cn/](https://www.luogu.com.cn/) 这个站"

    assert extract_http_urls(text) == ["https://www.luogu.com.cn/"]


def test_a_non_ascii_path_is_percent_encoded() -> None:
    """A URL may legally carry non-ASCII; the request line may not."""

    assert normalize_url("https://example.com/中文") == (
        "https://example.com/%E4%B8%AD%E6%96%87"
    )


def test_normalization_makes_two_spellings_one_page() -> None:
    same = [
        "https://Example.COM:443/page#section",
        "https://example.com/page",
    ]

    assert len(unique_urls(same)) == 1


def test_normalization_keeps_the_query_string() -> None:
    """Different query strings are different documents."""

    urls = unique_urls(
        ["https://example.com/s?q=1", "https://example.com/s?q=2"]
    )

    assert len(urls) == 2


def test_a_url_the_user_never_wrote_is_not_trusted() -> None:
    request = "读一下 https://example.com/real"

    assert trusted_urls(
        ["https://example.com/real", "https://example.com/invented"], request
    ) == ["https://example.com/real"]


def test_the_users_own_urls_come_first() -> None:
    request = "对比 https://a.example.com/x 和 https://b.example.com/y"

    assert trusted_urls([], request) == [
        "https://a.example.com/x",
        "https://b.example.com/y",
    ]


def test_alias_names_resolve_to_their_domains() -> None:
    aliases = {"飞书开放平台": ("open.feishu.cn",)}

    assert resolve_platform_domains(
        ["飞书开放平台"], request="搜索飞书开放平台", aliases=aliases
    ) == ["open.feishu.cn"]


def test_an_invented_domain_is_refused() -> None:
    aliases = {"飞书开放平台": ("open.feishu.cn",)}

    assert (
        resolve_platform_domains(
            ["tracker.example.com"], request="搜索公开资料", aliases=aliases
        )
        == []
    )
    assert resolve_platform_domains(
        ["tracker.example.com"],
        request="只搜 tracker.example.com",
        aliases=aliases,
    ) == ["tracker.example.com"]


def test_the_shipped_alias_table_is_loadable_and_reviewable() -> None:
    aliases = load_aliases()

    assert aliases["飞书开放平台"] == ("open.feishu.cn",)
    assert "open.feishu.cn" in known_domains(aliases)


def test_queries_are_cleaned_and_capped() -> None:
    assert normalize_queries(["  a   b ", "a b", "", "c", "d"], limit=2) == ["a b", "c"]


def test_site_filter_wraps_one_domain_plainly() -> None:
    assert with_site_filter("回调", ["open.feishu.cn"]) == "site:open.feishu.cn 回调"
    assert with_site_filter("回调", ["a.cn", "b.cn"]).startswith("(site:a.cn OR site:b.cn)")
    assert with_site_filter("回调", []) == "回调"


def test_domains_are_found_in_a_request() -> None:
    assert "open.feishu.cn" in domains_in_text("看看 open.feishu.cn 的文档")


# --- extraction helpers ----------------------------------------------------


def test_normalize_and_clip_do_not_cut_mid_word() -> None:
    assert normalize_text("  a  b \n\n c ") == "a b\nc"
    assert clip("alpha beta gamma", 12).endswith("beta")


def test_a_quote_is_a_substantive_sentence_not_navigation_chrome() -> None:
    text = (
        "Theme Auto Light Dark\n"
        "Table of Contents\n"
        "json — JSON encoder and decoder\n"
        "This module exposes an API familiar to users of the standard library.\n"
    )

    quote = select_quote(text, "这个模块提供什么 API")

    assert quote == "This module exposes an API familiar to users of the standard library."


def test_a_quote_falls_back_when_every_sentence_is_short() -> None:
    assert select_quote("一。二。三。", "问题") in {"一。", "二。", "三。"}


# --- search ----------------------------------------------------------------


def test_search_asks_for_json_and_returns_ranked_hits() -> None:
    captured: dict[str, str] = {}
    payload = {
        "results": [
            {"url": "https://a.example.com/1", "title": "A", "content": "片段 A"},
            {"url": "https://b.example.com/2", "title": "B", "content": "片段 B"},
            {"url": "https://a.example.com/1", "title": "dup", "content": "重复"},
        ]
    }

    class Provider(SearxngSearchProvider):
        def _open(self, request):
            captured["url"] = request.full_url
            return StubResponse(json.dumps(payload))

    results = Provider("http://searxng:8080").search(
        "回调", top_k=5, include_domains=["open.feishu.cn"], language="zh-CN"
    )

    assert "format=json" in captured["url"]
    assert "site%3Aopen.feishu.cn" in captured["url"]
    assert [(item.url, item.rank) for item in results] == [
        ("https://a.example.com/1", 1),
        ("https://b.example.com/2", 2),
    ]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, SearchUnavailable), (503, SearchUnavailable), (404, SearchError)],
)
def test_search_status_decides_whether_a_retry_can_help(status, expected) -> None:
    class Provider(SearxngSearchProvider):
        def _open(self, request):
            raise urllib.error.HTTPError(request.full_url, status, "boom", {}, None)

    with pytest.raises(expected):
        Provider("http://searxng:8080").search(
            "回调", top_k=5, include_domains=[], language="zh-CN"
        )


def test_search_rejects_html_instead_of_failing_later() -> None:
    """A SearXNG without JSON output must be reported, not silently empty."""

    class Provider(SearxngSearchProvider):
        def _open(self, request):
            return StubResponse("<html>not json</html>")

    with pytest.raises(SearchError):
        Provider("http://searxng:8080").search(
            "回调", top_k=5, include_domains=[], language="zh-CN"
        )


def test_an_unconfigured_search_is_retryable_not_silent() -> None:
    with pytest.raises(SearchUnavailable):
        SearxngSearchProvider("").search(
            "回调", top_k=5, include_domains=[], language="zh-CN"
        )


def test_merge_keeps_the_best_rank_per_url() -> None:
    from app.infrastructure.search.client import SearchResult

    groups = [
        [SearchResult(url="https://a/1", rank=3), SearchResult(url="https://b/2", rank=1)],
        [SearchResult(url="https://a/1", rank=1)],
    ]

    merged = merge_results(groups, top_k=2)

    assert [item.url for item in merged] == ["https://a/1", "https://b/2"]


# --- rendering -------------------------------------------------------------


def test_render_parses_markdown_and_links() -> None:
    payload = {
        "url": "https://a.example.com/1",
        "final_url": "https://a.example.com/1",
        "status": 200,
        "title": "标题",
        "markdown": "# 正文",
        "links": ["https://a.example.com/2"],
    }

    class Client(Crawl4AIClient):
        def _open(self, request):
            return StubResponse(json.dumps(payload))

    page = Client("http://crawl4ai:11235").render("https://a.example.com/1")

    assert page.markdown == "# 正文"
    assert page.links == ["https://a.example.com/2"]


def test_render_posts_to_md_with_the_bearer_token() -> None:
    """The official image secures every endpoint; an unauthenticated call 401s."""

    captured: dict = {}

    class Client(Crawl4AIClient):
        def _open(self, request):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = json.loads((request.data or b"{}").decode("utf-8"))
            # /md answers with the Markdown alone: no title, no links, no status.
            return StubResponse(json.dumps({"markdown": "# 只有正文"}))

    page = Client("http://crawl4ai:11235", api_token="t0k").render(
        "https://a.example.com/1"
    )

    assert captured["url"] == "http://crawl4ai:11235/md"
    assert captured["auth"] == "Bearer t0k"
    assert captured["body"] == {"url": "https://a.example.com/1"}
    assert page.markdown == "# 只有正文"
    assert page.links == []
    assert page.final_url == "https://a.example.com/1"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, RenderUnavailable), (500, RenderUnavailable), (400, RenderError)],
)
def test_render_status_decides_whether_a_retry_can_help(status, expected) -> None:
    class Client(Crawl4AIClient):
        def _open(self, request):
            raise urllib.error.HTTPError(request.full_url, status, "boom", {}, None)

    with pytest.raises(expected):
        Client("http://crawl4ai:11235").render("https://a.example.com/1")


def test_an_unconfigured_renderer_is_retryable_not_silent() -> None:
    with pytest.raises(RenderUnavailable):
        Crawl4AIClient("").render("https://a.example.com/1")


# --- reading ---------------------------------------------------------------


LONG_HTML = "<html><body><p>" + "正文内容。" * 60 + "</p></body></html>"


def test_a_complete_static_body_is_never_rendered() -> None:
    class Renderer:
        def render(self, url):  # pragma: no cover - must not be called
            raise AssertionError("renderer must not be used")

    reader = ContentReader(
        FakePageFetcher({"https://a.example.com/1": {"content": LONG_HTML}}),
        renderer=Renderer(),
        min_text_chars=50,
    )

    page = reader.read("https://a.example.com/1")

    assert page.fetch_method == "static"


def test_a_failed_render_falls_back_to_the_short_static_body() -> None:
    """Half a page beats no page when the browser sidecar is having a day."""

    class Renderer:
        def render(self, url):
            raise RenderUnavailable("crawl4ai is down")

    reader = ContentReader(
        FakePageFetcher({"https://a.example.com/1": {"content": "<p>短</p>"}}),
        renderer=Renderer(),
        min_text_chars=500,
    )

    page = reader.read("https://a.example.com/1")

    assert page.fetch_method == "static"
    assert page.text == "短"
    # The degradation is reported, not silent: a thin evidence record needs an
    # explanation, and the renderer's failure is that explanation.
    assert any("rendering failed" in note for note in page.notes)


def test_when_both_paths_fail_the_error_names_both() -> None:
    """The actionable half used to be swallowed by the renderer's failure."""

    class FailingFetcher(FakePageFetcher):
        def fetch(self, url, *, max_bytes=None):
            raise PageFetchRetryable("page fetch failed (503)", code="http_error")

    class Renderer:
        def render(self, url):
            raise RenderUnavailable("connection refused")

    reader = ContentReader(
        FailingFetcher(), renderer=Renderer(), min_text_chars=500
    )

    with pytest.raises(ContentUnavailable) as excinfo:
        reader.read("https://a.example.com/1")

    message = str(excinfo.value)
    assert "503" in message          # the static reason
    assert "connection refused" in message  # the render reason


def test_a_pdf_is_refused_rather_than_treated_as_html() -> None:
    from app.infrastructure.web.content_reader import UnsupportedDocument

    reader = ContentReader(
        FakePageFetcher(
            {
                "https://a.example.com/1": {
                    "content": "%PDF-1.4",
                    "content_type": "application/pdf",
                }
            }
        ),
        renderer=None,
    )

    with pytest.raises(UnsupportedDocument):
        reader.read("https://a.example.com/1")
