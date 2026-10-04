"""web.research: argument trust, source derivation, evidence and budgets.

The capability is the only public-web surface the Planner can reach, so these
tests pin the two things that make it safe: nothing gets fetched or searched
unless the user's own words asked for it, and nothing becomes evidence unless
it was actually read.
"""

from __future__ import annotations

import pytest

from app.capabilities.web_research import (
    WebResearchCapability,
    WebResearchError,
    WebResearchInput,
    WebResearchUrlUnreadable,
)
from app.infrastructure.search.client import SearchResult, SearchUnavailable
from app.infrastructure.web.content_reader import ContentReader
from app.infrastructure.web.crawl4ai_client import RenderedPage, RenderUnavailable
from app.infrastructure.web.evidence import EvidenceBuilder
from app.providers.page_fetcher import PageFetchError, PageFetchRetryable
from app.testing.fake_providers import FakePageFetcher

USER_URL = "https://93.184.216.34/protocol"
OTHER_URL = "https://93.184.216.34/other"
PAGE = "<html><head><title>北京市网络协议</title></head><body><p>" + "正文内容。" * 80 + "</p></body></html>"


class FakeSearchProvider:
    """Returns canned hits per query and records the queries it was asked."""

    def __init__(self, hits=None, *, error: Exception | None = None) -> None:
        self.hits = list(hits or [])
        self.error = error
        self.calls: list[dict] = []

    def search(self, query, *, top_k, include_domains, language):
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "domains": list(include_domains),
                "language": language,
            }
        )
        if self.error is not None:
            raise self.error
        return [
            # Real engines put the query in the result text; the capability now
            # drops hits that mention nothing the query asked about.
            SearchResult(
                url=url,
                title=f"{query} - hit {index}",
                rank=index,
                snippet=f"{query} 的相关摘要",
            )
            for index, url in enumerate(self.hits[:top_k], start=1)
        ]


class FakeRenderer:
    """Stands in for the Crawl4AI sidecar."""

    def __init__(self, pages=None, *, error: Exception | None = None) -> None:
        self.pages = dict(pages or {})
        self.error = error
        self.calls: list[str] = []

    def render(self, url: str) -> RenderedPage:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return RenderedPage(
            url=url,
            final_url=url,
            status=200,
            title="渲染后的标题",
            markdown=str(self.pages.get(url, "# 渲染正文\n" + "渲染内容。" * 60)),
        )


def build_capability(
    *,
    pages=None,
    search=None,
    renderer=None,
    aliases=None,
    **kwargs,
) -> WebResearchCapability:
    fetcher = FakePageFetcher(pages or {})
    return WebResearchCapability(
        ContentReader(fetcher, renderer=renderer, min_text_chars=kwargs.pop("min_text_chars", 500)),
        search_provider=search,
        evidence_builder=EvidenceBuilder(page_chars=kwargs.pop("page_chars", 8000)),
        aliases=aliases if aliases is not None else {"飞书开放平台": ("open.feishu.cn",)},
        **kwargs,
    )


def test_a_url_the_model_invented_is_not_fetched() -> None:
    """Only links the user wrote may be fetched, whatever the Planner says."""

    fetcher = FakePageFetcher({USER_URL: {"content": PAGE}})
    capability = WebResearchCapability(
        ContentReader(fetcher, renderer=None, min_text_chars=1),
        search_provider=None,
        aliases={},
    )

    result = capability.execute(
        capability.validate(
            {
                "request": f"读一下 {USER_URL}",
                "urls": [USER_URL, "https://evil.example.com/planted"],
            }
        )
    )

    assert result["urls"] == [USER_URL]
    assert [call[0] for call in fetcher.calls] == [USER_URL]


def test_a_request_without_any_source_is_refused() -> None:
    capability = build_capability()

    with pytest.raises(WebResearchError) as excinfo:
        capability.execute(capability.validate({"request": "帮我看看这个"}))

    assert excinfo.value.code == "no_research_source"


def test_reading_one_link_produces_the_documented_evidence_shape() -> None:
    capability = build_capability(pages={USER_URL: {"content": PAGE}})

    result = capability.execute(
        capability.validate({"request": f"读一下 {USER_URL}", "urls": [USER_URL]})
    )

    assert result["evidence"]
    evidence = result["evidence"][0]
    assert set(evidence) >= {
        "evidence_id",
        "source_type",
        "title",
        "url",
        "quote",
        "text",
        "content_hash",
        "retrieved_at",
        "fetch_method",
    }
    assert evidence["evidence_id"].startswith("ev-")
    assert evidence["url"] == USER_URL
    assert evidence["content_hash"].startswith("sha256:")
    assert evidence["fetch_method"] == "static"
    assert result["queries"] == []


def test_the_same_page_yields_the_same_evidence_id() -> None:
    """A retry must not look like a second, different source."""

    capability = build_capability(pages={USER_URL: {"content": PAGE}})
    payload = capability.validate({"request": f"读一下 {USER_URL}", "urls": [USER_URL]})

    first = capability.execute(payload)
    second = capability.execute(payload)

    assert first["evidence"][0]["evidence_id"] == second["evidence"][0]["evidence_id"]


def test_search_hits_are_read_before_they_become_evidence() -> None:
    """A search snippet is a ranking hint; only the fetched body is citable."""

    search = FakeSearchProvider([OTHER_URL])
    capability = build_capability(pages={OTHER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate({"request": "搜索一下北京市网络协议", "queries": ["北京市网络协议"]})
    )

    assert [call["query"] for call in search.calls] == ["北京市网络协议"]
    assert result["evidence"][0]["url"] == OTHER_URL
    assert "摘要" not in result["evidence"][0]["text"]


def test_a_platform_name_becomes_a_domain_only_when_the_table_knows_it() -> None:
    search = FakeSearchProvider([OTHER_URL])
    capability = build_capability(pages={OTHER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate(
            {
                "request": "搜索飞书开放平台的消息卡片回调配置",
                "queries": ["消息卡片 回调配置"],
                "include_domains": ["飞书开放平台", "unknown-platform"],
            }
        )
    )

    assert result["include_domains"] == ["open.feishu.cn"]
    assert search.calls[0]["domains"] == ["open.feishu.cn"]
    assert any("include_domains" in warning for warning in result["warnings"])


def test_a_domain_the_user_wrote_is_accepted() -> None:
    search = FakeSearchProvider([OTHER_URL])
    capability = build_capability(pages={OTHER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate(
            {
                "request": "搜索 open.feishu.cn 上的回调文档",
                "queries": ["回调"],
                "include_domains": ["open.feishu.cn", "evil.example.com"],
            }
        )
    )

    assert result["include_domains"] == ["open.feishu.cn"]


def test_user_link_and_search_hits_are_merged_within_the_page_budget() -> None:
    search = FakeSearchProvider([OTHER_URL, "https://93.184.216.34/third"])
    capability = build_capability(
        pages={
            USER_URL: {"content": PAGE},
            OTHER_URL: {"content": PAGE},
            "https://93.184.216.34/third": {"content": PAGE},
        },
        search=search,
    )

    result = capability.execute(
        capability.validate(
            {
                "request": f"读一下 {USER_URL}，再搜索公开资料",
                "urls": [USER_URL],
                "queries": ["公开资料"],
                "max_pages": 2,
            }
        )
    )

    assert [item["url"] for item in result["evidence"]] == [USER_URL, OTHER_URL]


def test_an_unreadable_user_url_is_not_replaced_by_search_evidence() -> None:
    search = FakeSearchProvider([OTHER_URL])
    capability = build_capability(
        pages={OTHER_URL: {"content": PAGE}},
        search=search,
    )

    with pytest.raises(WebResearchUrlUnreadable) as excinfo:
        capability.execute(
            capability.validate(
                {
                    "request": f"读一下 {USER_URL}，再搜索公开资料",
                    "urls": [USER_URL],
                    "queries": ["公开资料"],
                }
            )
        )

    assert excinfo.value.code == "explicit_url_unreadable"
    assert USER_URL in str(excinfo.value)
    assert "登录" in str(excinfo.value)


def test_queries_are_capped_and_deduplicated() -> None:
    search = FakeSearchProvider([OTHER_URL])
    capability = build_capability(pages={OTHER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate(
            {
                "request": "搜索",
                "queries": ["一", "  ", "二", "二", "三", "四"],
            }
        )
    )

    assert result["queries"] == ["一", "二", "三"]
    assert len(search.calls) == 3


def test_one_dead_page_does_not_fail_the_task() -> None:
    search = FakeSearchProvider([OTHER_URL, "https://93.184.216.34/dead"])
    capability = build_capability(pages={OTHER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate({"request": "搜索公开资料", "queries": ["资料"]})
    )

    assert [item["url"] for item in result["evidence"]] == [OTHER_URL]
    assert any("dead" in warning for warning in result["warnings"])


def test_every_page_failing_is_a_capability_error() -> None:
    search = FakeSearchProvider(["https://93.184.216.34/dead"])
    capability = build_capability(search=search)

    with pytest.raises(WebResearchError) as excinfo:
        capability.execute(
            capability.validate({"request": "搜索公开资料", "queries": ["资料"]})
        )

    assert excinfo.value.code == "no_evidence"


def test_a_search_outage_still_leaves_a_user_link_usable() -> None:
    search = FakeSearchProvider(error=SearchUnavailable("searxng is down"))
    capability = build_capability(pages={USER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate(
            {"request": f"读一下 {USER_URL} 并搜索资料", "urls": [USER_URL], "queries": ["资料"]}
        )
    )

    assert result["evidence"][0]["url"] == USER_URL
    assert any("搜索暂不可用" in warning for warning in result["warnings"])


def test_a_document_is_skipped_rather_than_read_as_a_web_page() -> None:
    """PDFs belong to the RAG pipeline; the rest of the task still finishes."""

    search = FakeSearchProvider(["https://93.184.216.34/report.pdf", OTHER_URL])
    capability = build_capability(pages={OTHER_URL: {"content": PAGE}}, search=search)

    result = capability.execute(
        capability.validate({"request": "搜索公开资料", "queries": ["资料"]})
    )

    assert [item["url"] for item in result["evidence"]] == [OTHER_URL]
    assert any("report.pdf" in warning for warning in result["warnings"])


def test_a_short_static_body_is_rendered_when_the_renderer_can() -> None:
    renderer = FakeRenderer()
    capability = build_capability(
        pages={USER_URL: {"content": "<html><body>加载中</body></html>"}},
        renderer=renderer,
    )

    result = capability.execute(
        capability.validate({"request": f"读一下 {USER_URL}", "urls": [USER_URL]})
    )

    assert renderer.calls == [USER_URL]
    assert result["evidence"][0]["fetch_method"] == "crawl4ai"


def test_a_degraded_read_says_why_the_evidence_is_thin() -> None:
    """Read from a short static body because rendering failed: say so."""

    renderer = FakeRenderer(error=RenderUnavailable("crawl4ai is down"))
    capability = build_capability(
        pages={USER_URL: {"content": "<html><body><p>只有一小段</p></body></html>"}},
        renderer=renderer,
    )

    result = capability.execute(
        capability.validate({"request": f"读一下 {USER_URL}", "urls": [USER_URL]})
    )

    assert result["evidence"][0]["fetch_method"] == "static"
    assert any("rendering failed" in warning for warning in result["warnings"])


def test_a_404_is_not_sent_to_the_renderer() -> None:
    class FailingFetcher(FakePageFetcher):
        def fetch(self, url, *, max_bytes=None):
            error = PageFetchError("page fetch rejected (404)", code="http_error")
            error.status = 404
            raise error

    renderer = FakeRenderer()
    capability = WebResearchCapability(
        ContentReader(FailingFetcher(), renderer=renderer, min_text_chars=1),
        search_provider=None,
        aliases={},
    )

    with pytest.raises(WebResearchError):
        capability.execute(
            capability.validate({"request": f"读一下 {USER_URL}", "urls": [USER_URL]})
        )

    assert renderer.calls == []


def test_a_transient_fetch_failure_falls_back_to_the_renderer() -> None:
    class FailingFetcher(FakePageFetcher):
        def fetch(self, url, *, max_bytes=None):
            raise PageFetchRetryable("page fetch failed: timeout")

    renderer = FakeRenderer()
    capability = WebResearchCapability(
        ContentReader(FailingFetcher(), renderer=renderer, min_text_chars=1),
        search_provider=None,
        aliases={},
    )

    result = capability.execute(
        capability.validate({"request": f"读一下 {USER_URL}", "urls": [USER_URL]})
    )

    assert renderer.calls == [USER_URL]
    assert result["evidence"][0]["fetch_method"] == "crawl4ai"


def test_the_evidence_budget_is_charged_by_characters() -> None:
    long_page = "<html><body>" + "长正文。" * 900 + "</body></html>"
    capability = build_capability(
        pages={USER_URL: {"content": long_page}, OTHER_URL: {"content": long_page}},
        page_chars=500,
        max_evidence_chars=600,
    )

    result = capability.execute(
        capability.validate(
            {
                "request": f"读一下 {USER_URL} 和 {OTHER_URL}",
                "urls": [USER_URL, OTHER_URL],
            }
        )
    )

    assert len(result["evidence"]) == 1
    assert any("上限" in warning for warning in result["warnings"])


def test_the_capability_clamps_what_a_plan_may_ask_for() -> None:
    search = FakeSearchProvider([OTHER_URL, "https://93.184.216.34/third"])
    capability = build_capability(
        pages={
            OTHER_URL: {"content": PAGE},
            "https://93.184.216.34/third": {"content": PAGE},
        },
        search=search,
        max_results=1,
        max_pages=1,
    )

    result = capability.execute(
        capability.validate(
            {"request": "搜索资料", "queries": ["资料"], "max_results": 9, "max_pages": 9}
        )
    )

    assert search.calls[0]["top_k"] == 1
    assert len(result["evidence"]) == 1


def test_unknown_arguments_are_rejected() -> None:
    from pydantic import ValidationError

    capability = build_capability()

    with pytest.raises(ValidationError):
        capability.validate({"request": "搜索", "mode": "search"})


def test_the_descriptor_exposes_only_the_validated_arguments() -> None:
    descriptor = WebResearchCapability(
        ContentReader(FakePageFetcher(), renderer=None), aliases={}
    ).descriptor

    assert descriptor.name == "web.research"
    assert descriptor.side_effect is False
    assert descriptor.requires_approval is False
    assert descriptor.input_schema == WebResearchInput.model_json_schema()


class FixedHitsProvider:
    """Returns exactly the hits it was given, whatever the query."""

    def __init__(self, hits) -> None:
        self.hits = list(hits)

    def search(self, query, *, top_k, include_domains, language):
        return list(self.hits[:top_k])


def test_search_hits_unrelated_to_the_query_are_not_evidence() -> None:
    """Observed in production: a question about Feishu came back with a used-car
    site. The answer step then reported "the sources do not mention Feishu",
    which looked like a search that had run but not worked."""

    provider = FixedHitsProvider(
        [
            SearchResult(
                url="https://www.carfax.com/cars-for-sale",
                title="Used Cars for Sale",
                snippet="Browse used cars in Seattle",
                rank=1,
            ),
            SearchResult(
                url=OTHER_URL,
                title="飞书开放平台 功能介绍",
                snippet="飞书开放平台提供的服务端 API 与卡片能力",
                rank=2,
            ),
        ]
    )
    capability = build_capability(
        pages={OTHER_URL: {"content": PAGE}}, search=provider
    )

    result = capability.execute(
        capability.validate(
            {"request": "搜索飞书开放平台的主要内容", "queries": ["飞书开放平台 主要内容"]}
        )
    )

    assert [item["url"] for item in result["evidence"]] == [OTHER_URL]
    assert any("不相关" in warning for warning in result["warnings"])
    assert any("carfax" in warning or "已丢弃" in warning for warning in result["warnings"])


def test_a_search_that_only_returns_noise_is_retried_not_accepted() -> None:
    provider = FixedHitsProvider(
        [
            SearchResult(
                url="https://www.carfax.com/cars-for-sale",
                title="Used Cars for Sale",
                snippet="Browse used cars",
                rank=1,
            )
        ]
    )
    capability = build_capability(search=provider)

    with pytest.raises(WebResearchError) as excinfo:
        capability.execute(
            capability.validate(
                {"request": "搜索飞书开放平台的主要内容", "queries": ["飞书开放平台 主要内容"]}
            )
        )

    # Retryable: the vendor's result set is often junk for one call and fine on
    # the next, so the kernel gets to try again.
    assert excinfo.value.classification == "retryable_error"
    assert excinfo.value.code == "no_relevant_results"


def test_a_user_url_still_counts_when_the_search_only_returns_noise() -> None:
    provider = FixedHitsProvider(
        [
            SearchResult(
                url="https://www.carfax.com/cars-for-sale",
                title="Used Cars for Sale",
                snippet="Browse used cars",
                rank=1,
            )
        ]
    )
    capability = build_capability(
        pages={USER_URL: {"content": PAGE}}, search=provider
    )

    result = capability.execute(
        capability.validate(
            {
                "request": f"读一下 {USER_URL} 并搜索飞书开放平台的主要内容",
                "urls": [USER_URL],
                "queries": ["飞书开放平台 主要内容"],
            }
        )
    )

    assert [item["url"] for item in result["evidence"]] == [USER_URL]
    assert any("不相关" in warning for warning in result["warnings"])
