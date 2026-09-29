"""web.fetch and web.extract: read-only access to public pages.

Two capabilities rather than one, because they answer two different
questions: fetch gets the bytes, extract makes sense of them. Splitting them
also gives the Plan a real reference to carry - the extract step reads the
fetch step output through $steps.<step_id>.output.content - which is what the
dynamic planner has to compose.
"""

from __future__ import annotations

import hashlib
from datetime import timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field

from app.kernel.models import (
    CapabilityDescriptor,
    CapabilityInputBinding,
    StepOutputRef,
)

CAPABILITY_FETCH = "web.fetch"
CAPABILITY_EXTRACT = "web.extract"
DEFAULT_TIMEOUT_SECONDS = 10
EVIDENCE_VERSION = 1
_SNIPPET_CHARS = 280

# Tags whose text is markup rather than content.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
# Tags that end a line of prose; without them the whole page becomes one line.
_BREAK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "pre",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)


class WebFetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    max_bytes: int | None = Field(default=None, gt=0, le=8 * 1024 * 1024)


class WebFetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str
    status: int
    content_type: str
    content: str
    truncated: bool
    fetched_at: str


class WebExtractInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: str = Field(min_length=1)
    url: str | None = Field(default=None, max_length=2000)


class WebExtractPlanInput(BaseModel):
    """The Planner-facing shape of web.extract.

    A model writing a plan cannot carry the fetched bytes in its answer, so it
    names the step that has them. The Runtime binder turns that name into the
    ``document`` argument this capability actually reads; because the reference
    is required, a plan cannot reach execution with nothing to extract.
    """

    model_config = ConfigDict(extra="forbid")

    document_ref: StepOutputRef = Field(
        description="引用更早 web.fetch 步骤输出的 content"
    )
    url: str | None = Field(default=None, max_length=2000)


class WebExtractResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    text: str
    links: list[str]
    evidence: list[dict[str, Any]]


class _ReadableTextExtractor(HTMLParser):
    """Conservative text extraction: no layout, no guessing, no scripts."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.lower().startswith(("javascript:", "mailto:")):
                resolved = urljoin(self.base_url, href) if self.base_url else href
                if resolved not in self.links:
                    self.links.append(resolved)
        if tag in _BREAK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in _BREAK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)


def normalize_text(value: str) -> str:
    """Collapse whitespace while keeping paragraph breaks readable."""

    lines = [" ".join(line.split()) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)


def build_evidence(
    *, url: str | None, title: str, text: str, source: str | None = None
) -> dict[str, Any]:
    """One provenance record per extracted document.

    The id is derived from the content instead of a fresh uuid: a retry of the
    same step produces the same id, so the evidence table stays idempotent.
    """

    digest = hashlib.sha256(
        f"{url or ''}|{title}|{text[:_SNIPPET_CHARS]}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "evidence_id": f"ev-{digest}",
        "source": source or ("web" if url else "document"),
        "title": title or url or "document",
        "url": url,
        "snippet": text[:_SNIPPET_CHARS],
        "version": EVIDENCE_VERSION,
    }


class WebFetchCapability:
    """Fetches one public page; the fetcher owns the SSRF guard."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_FETCH,
        description=(
            "抓取一个公开网页的原文（只读）。只支持 http/https 公开地址，"
            "响应上限 512KB。支持 text/html、text/plain、text/markdown 文本响应；"
            "文档站常提供 .md 版本，内容比 HTML 壳更完整。"
            "输出里的 content 交给 web.extract 处理。"
        ),
        input_schema=WebFetchInput.model_json_schema(),
        output_schema=WebFetchResult.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )

    def __init__(self, fetcher, *, timeout_seconds: int | None = None) -> None:
        self.fetcher = fetcher
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> WebFetchInput:
        return WebFetchInput.model_validate(arguments)

    def execute(self, arguments: WebFetchInput) -> dict[str, Any]:
        document = self.fetcher.fetch(arguments.url, max_bytes=arguments.max_bytes)
        return WebFetchResult(
            url=document.url,
            final_url=document.final_url,
            status=int(document.status),
            content_type=document.content_type,
            content=document.content,
            truncated=bool(document.truncated),
            fetched_at=document.fetched_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        ).model_dump()


class WebExtractCapability:
    """Turns fetched bytes into title, text, links and one evidence record."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_EXTRACT,
        description=(
            "从网页或纯文本里抽出标题、正文、链接，并产出来源证据（只读）。"
            "正文来源用 document_ref 指向更早 web.fetch 步骤输出的 content。"
        ),
        input_schema=WebExtractInput.model_json_schema(),
        planner_input_schema=WebExtractPlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="document_ref",
                runtime_argument="document",
                source_capability=CAPABILITY_FETCH,
                source_output="content",
            )
        ],
        output_schema=WebExtractResult.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )

    def __init__(self, *, timeout_seconds: int | None = None) -> None:
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> WebExtractInput:
        return WebExtractInput.model_validate(arguments)

    def execute(self, arguments: WebExtractInput) -> dict[str, Any]:
        extractor = _ReadableTextExtractor(base_url=arguments.url)
        extractor.feed(arguments.document)
        extractor.close()
        title = normalize_text(" ".join(extractor.title_parts))
        text = normalize_text(" ".join(extractor.text_parts))
        if text and title and text.startswith(title):
            text = text[len(title):].lstrip()
        evidence = build_evidence(url=arguments.url, title=title, text=text)
        return WebExtractResult(
            title=title,
            text=text,
            links=list(extractor.links),
            evidence=[evidence],
        ).model_dump()
