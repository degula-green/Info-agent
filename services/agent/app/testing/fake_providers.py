"""Deterministic doubles for the Step-4 providers.

The platform tests never touch the network or a model: a page is a canned
string and an answer is a scripted draft, so a failure in the suite always
means this repository changed, never that a remote service had a bad day.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.providers.answer import AnswerDraft
from app.providers.page_fetcher import PageDocument, PageFetchError


class FakePageFetcher:
    """Serves canned pages by URL; an unknown URL is a permanent error."""

    def __init__(
        self,
        pages: dict[str, dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.pages = dict(pages or {})
        self.error = error
        self.calls: list[tuple[str, int | None]] = []

    def fetch(self, url: str, *, max_bytes: int | None = None) -> PageDocument:
        self.calls.append((url, max_bytes))
        if self.error is not None:
            raise self.error
        page = self.pages.get(url)
        if page is None:
            raise PageFetchError(f"no canned page for {url}", code="page_not_found")
        return PageDocument(
            url=url,
            final_url=str(page.get("final_url") or url),
            status=int(page.get("status") or 200),
            content_type=str(page.get("content_type") or "text/html"),
            content=str(page.get("content") or ""),
            truncated=bool(page.get("truncated") or False),
            fetched_at=datetime.now(timezone.utc),
        )


class FakeAnswerProvider:
    """Returns a scripted draft; citations default to the supplied evidence."""

    def __init__(
        self,
        answer: str = "基于资料整理的回答",
        *,
        citations: list[dict[str, Any]] | None = None,
        model_calls: int = 1,
        error: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.citations = citations
        self.model_calls = model_calls
        self.error = error
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def compose(self, question: str, evidence: list[dict[str, Any]]) -> AnswerDraft:
        self.calls.append((question, [dict(item) for item in evidence]))
        if self.error is not None:
            raise self.error
        citations = self.citations
        if citations is None:
            citations = [
                {
                    "evidence_id": item.get("evidence_id"),
                    "quote": str(item.get("snippet") or "")[:80],
                }
                for item in evidence
                if isinstance(item, dict) and item.get("evidence_id")
            ]
        return AnswerDraft(
            answer=self.answer,
            citations=[dict(item) for item in citations],
            model_calls=self.model_calls,
        )
