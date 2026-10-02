"""web.fetch and web.extract: internal read-only building blocks.

These two used to be Planner-visible. ``web.research`` now owns public-web
reading end to end, so the Planner sees one capability instead of a fetch step
and an extract step it has to wire together; both classes stay because the
kernel tests and the research pipeline compose them, and because the fetch
step is still the single place the SSRF guard is applied.
"""

from __future__ import annotations

import hashlib
from datetime import timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.web.extractor import extract_document, normalize_text
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
        title, text, links = extract_document(arguments.document, url=arguments.url)
        evidence = build_evidence(url=arguments.url, title=title, text=text)
        return WebExtractResult(
            title=title,
            text=text,
            links=links,
            evidence=[evidence],
        ).model_dump()
