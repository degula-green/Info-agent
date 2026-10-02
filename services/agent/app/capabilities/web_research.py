"""web.research: the one public-web capability the Planner may choose.

Search, fetch, render and extraction are internal steps. Exposing them
separately let a model plan its way into a fetch of a URL it invented, or into
an extract step with nothing to extract; one capability with a validated
argument set removes that whole class of plan. It also makes "did we actually
read the page we are about to quote" a property of a single, testable unit.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.search.client import SearchError, SearchUnavailable, merge_results
from app.infrastructure.web.content_reader import (
    ContentReadError,
    ContentReader,
    UnsupportedDocument,
)
from app.infrastructure.web.evidence import EvidenceBuilder
from app.infrastructure.web.url_tools import (
    load_aliases,
    normalize_queries,
    resolve_platform_domains,
    trusted_urls,
)
from app.kernel.models import CapabilityDescriptor

CAPABILITY_NAME = "web.research"
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_PAGES = 3
DEFAULT_MAX_QUERIES = 3
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_MAX_EVIDENCE_CHARS = 60_000
_QUERY_SPLIT = re.compile(r"[\s,，。、;；:：!！?？/|]+")


def _relevance_keys(query: str) -> set[str]:
    """Keys that make a search hit plausibly about the query.

    A whole whitespace token is too strict for Chinese -- "飞书开放平台" would not
    match a page that only says "飞书" -- while single characters are too loose
    for English. So non-ASCII runs contribute their two-character grams and
    ASCII words their whole lowercase form.
    """

    keys: set[str] = set()
    for token in _QUERY_SPLIT.split(query.lower()):
        if not token:
            continue
        if token.isascii():
            if len(token) >= 3:
                keys.add(token)
            continue
        for index in range(len(token) - 1):
            keys.add(token[index : index + 2])
    return keys


def _looks_relevant(result, query: str) -> bool:
    """Whether the hit's own text mentions anything the query asked about."""

    keys = _relevance_keys(query)
    if not keys:
        return True
    haystack = f"{result.title} {result.snippet}".strip().lower()
    if not haystack:
        # Nothing to judge on: keep what the engine ranked rather than silently
        # dropping it.
        return True
    return any(key in haystack for key in keys)


class WebResearchError(RuntimeError):
    """Base error; the classification attribute is what the kernel reads."""

    classification = "permanent_error"
    code = "web_research_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class WebResearchUnavailable(WebResearchError):
    """Transient failure: the same research request may work next time."""

    classification = "retryable_error"
    code = "web_research_unavailable"


class WebResearchInput(BaseModel):
    """What the Planner may hand over; no mode, the arguments imply the path."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=4000)
    urls: list[str] = Field(default_factory=list, max_length=10)
    # The Planner may send more terms than this deployment allows; the extra
    # ones are dropped during normalization instead of failing the whole plan,
    # because a slightly greedy model is not a contract violation.
    queries: list[str] = Field(default_factory=list, max_length=10)
    include_domains: list[str] = Field(default_factory=list, max_length=5)
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=20)
    max_pages: int = Field(default=DEFAULT_MAX_PAGES, ge=1, le=10)


class WebResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str
    urls: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    include_domains: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WebResearchCapability:
    """Searches, reads and produces evidence; it never writes an answer."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description=(
            "检索公开网页并产出可引用的证据（只读）。读取用户给出的链接、"
            "按关键词搜索公开网页，或两者混合。搜索摘要不作为最终依据，"
            "只有实际抓取到的正文才会成为证据。参数 urls 只接受用户原话中"
            "出现过的链接，include_domains 只接受受控平台名或用户写明的域名。"
            "request 由系统填入用户原话，不需要（也不应该）由你提供。"
        ),
        input_schema=WebResearchInput.model_json_schema(),
        output_schema=WebResearchResult.model_json_schema(),
        # URLs the user wrote are the only ones this capability may fetch, and
        # that check reads the request text -- so the request must be the user's
        # own words, not the model's paraphrase of them.
        task_text_argument="request",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )

    def __init__(
        self,
        reader: ContentReader,
        *,
        search_provider=None,
        evidence_builder: EvidenceBuilder | None = None,
        aliases: dict[str, tuple[str, ...]] | None = None,
        language: str = DEFAULT_LANGUAGE,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_queries: int = DEFAULT_MAX_QUERIES,
        max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS,
        timeout_seconds: int | None = None,
    ) -> None:
        self.reader = reader
        self.search_provider = search_provider
        self.evidence_builder = evidence_builder or EvidenceBuilder()
        # ``None`` means "read the reviewed table from disk"; an empty dict is
        # an explicit "no aliases in this deployment" and must stay empty.
        self.aliases = load_aliases() if aliases is None else dict(aliases)
        self.language = str(language or DEFAULT_LANGUAGE)
        # Deployment ceilings: the Planner may ask for fewer pages, never more,
        # so a single plan cannot decide on its own how much egress to spend.
        self.max_results = max(int(max_results), 1)
        self.max_pages = max(int(max_pages), 1)
        self.max_queries = max(int(max_queries), 1)
        self.max_evidence_chars = max(int(max_evidence_chars), 1)
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> WebResearchInput:
        return WebResearchInput.model_validate(arguments)

    def execute(self, arguments: WebResearchInput) -> dict[str, Any]:
        request = arguments.request
        warnings: list[str] = []
        max_results = min(int(arguments.max_results), self.max_results)
        max_pages = min(int(arguments.max_pages), self.max_pages)
        urls = trusted_urls(arguments.urls, request)
        domains = resolve_platform_domains(
            arguments.include_domains, request=request, aliases=self.aliases
        )
        requested_domains = [
            str(item).strip() for item in arguments.include_domains if str(item).strip()
        ]
        if requested_domains and len(domains) < len(set(requested_domains)):
            # A silently dropped domain would turn a scoped search into an open
            # one, so the caller is told which scope it did not get.
            warnings.append(
                "include_domains 中部分名称不在别名表内、也不是用户写明的域名，已忽略"
            )
        queries = normalize_queries(arguments.queries, limit=self.max_queries)
        if not queries and not urls:
            # The Planner sometimes writes the search terms into `request` (the
            # argument it does not control) and leaves `queries` empty. Asking to
            # search without naming terms still means "search this", and the
            # user's own words are the only safe query, so it is used instead of
            # failing the whole task.
            queries = normalize_queries([request], limit=1)

        candidates: list[str] = []
        search_unavailable = False
        search_attempted = False
        if queries:
            candidates, search_warnings, search_unavailable, search_attempted = self._search(
                queries, domains, max_results
            )
            warnings.extend(search_warnings)
        if not urls and not candidates:
            if search_unavailable:
                # The search provider was the only planned source and it failed
                # transiently; that is worth another attempt, not a dead end.
                raise WebResearchUnavailable(
                    "搜索服务暂不可用：" + "；".join(warnings),
                    code="search_unavailable",
                )
            if search_attempted:
                # A search ran and produced nothing usable. That is usually a
                # transient vendor glitch (observed: a used-car site for a
                # question about Feishu), so it is worth one more attempt.
                raise WebResearchUnavailable(
                    "搜索没有返回可用结果：" + "；".join(warnings),
                    code="no_relevant_results",
                )
            raise WebResearchError(
                "web.research 需要用户给出的链接或可搜索的关键词",
                code="no_research_source",
            )

        targets = self._targets(urls, candidates, max_pages)
        evidence: list[dict[str, Any]] = []
        for url in targets:
            try:
                page = self.reader.read(url)
            except UnsupportedDocument as exc:
                warnings.append(f"{url}: {exc}")
                continue
            except ContentReadError as exc:
                warnings.append(f"{url}: {exc}")
                continue
            if not page.text.strip():
                warnings.append(f"{url}: 没有抽出可用正文")
                continue
            warnings.extend(f"{url}: {note}" for note in page.notes)
            evidence.append(
                self.evidence_builder.build(
                    url=page.final_url or url,
                    title=page.title,
                    text=page.text,
                    fetch_method=page.fetch_method,
                    question=request,
                    retrieved_at=page.retrieved_at,
                )
            )

        if not evidence:
            raise WebResearchError(
                "没有抓取到任何可用证据：" + ("；".join(warnings) or "无可读页面"),
                code="no_evidence",
            )
        evidence, budget_warnings = self._apply_budget(evidence)
        warnings.extend(budget_warnings)
        return WebResearchResult(
            request=request,
            urls=urls,
            queries=queries,
            include_domains=domains,
            evidence=evidence,
            warnings=warnings,
        ).model_dump()

    def _search(
        self, queries: list[str], domains: list[str], max_results: int
    ) -> tuple[list[str], list[str], bool, bool]:
        """Candidate URLs from every query; a search outage is not fatal alone."""

        warnings: list[str] = []
        unavailable = False
        if self.search_provider is None:
            return [], ["未配置搜索服务，仅使用用户给出的链接"], False, False
        groups = []
        for query in queries:
            try:
                found = self.search_provider.search(
                    query,
                    top_k=max_results,
                    include_domains=domains,
                    language=self.language,
                )
            except SearchUnavailable as exc:
                warnings.append(f"搜索暂不可用（{query}）：{exc}")
                unavailable = True
                continue
            except SearchError as exc:
                warnings.append(f"搜索失败（{query}）：{exc}")
                continue
            relevant = [item for item in found if _looks_relevant(item, query)]
            if len(relevant) < len(found):
                # Observed: a search for "飞书开放平台 主要内容" came back with a
                # used-car site. Evidence that answers nothing is worse than no
                # evidence -- it makes the answer step say the sources are silent
                # while looking like it searched.
                warnings.append(
                    f"搜索结果与查询不相关，已丢弃 {len(found) - len(relevant)} 条（{query}）"
                )
            groups.append(relevant)
        merged = merge_results(groups, top_k=max_results)
        return [item.url for item in merged], warnings, unavailable, True

    @staticmethod
    def _targets(urls: list[str], candidates: list[str], max_pages: int) -> list[str]:
        """User URLs first, then search hits, capped by the page budget."""

        ordered: list[str] = []
        for url in list(urls) + list(candidates):
            if url and url not in ordered:
                ordered.append(url)
        return ordered[: max(int(max_pages), 1)]

    def _apply_budget(
        self, evidence: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Caps the total body size handed to the answer step.

        The cap is on characters, not pages: one long page and three short ones
        should both fit, and the step that pays for the model call is the one
        that should decide.
        """

        kept: list[dict[str, Any]] = []
        used = 0
        warnings: list[str] = []
        for item in evidence:
            text = str(item.get("text") or "")
            if used + len(text) > self.max_evidence_chars and kept:
                warnings.append(f"证据总量超过上限，已丢弃 {item.get('url')}")
                continue
            kept.append(item)
            used += len(text)
        return kept, warnings
