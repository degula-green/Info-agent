"""Search result shape and provider protocol.

The snippet from a search engine is a ranking hint, never a citable source, so
it is deliberately not part of the evidence contract: only text that was
actually fetched may be quoted later.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class SearchError(RuntimeError):
    """Base error; the classification attribute is what the kernel reads."""

    classification = "permanent_error"
    code = "search_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class SearchUnavailable(SearchError):
    """Transient failure: the same query may work on the next attempt."""

    classification = "retryable_error"
    code = "search_unavailable"


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = ""
    snippet: str = ""
    # Search providers that can return the page body directly may set this.
    # Empty means the page still has to be fetched before it becomes evidence.
    raw_content: str = ""
    rank: int = Field(default=0, ge=0)
    provider: str = "searxng"
    published_at: str | None = None


class SearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        top_k: int,
        include_domains: list[str],
        language: str,
    ) -> list[SearchResult]:
        ...


def merge_results(groups: list[list[SearchResult]], *, top_k: int) -> list[SearchResult]:
    """One candidate list from several queries: normalized, deduped, ranked.

    A page found by two queries is one page; the best rank it ever reached is
    the one that decides where it lands, so a strong hit from a narrow query is
    not buried by a weak hit from a broad one.
    """

    best: dict[str, SearchResult] = {}
    for group in groups:
        for result in group:
            url = str(result.url or "").strip()
            if not url:
                continue
            current = best.get(url)
            if current is None or (result.rank or 0) < (current.rank or 0):
                best[url] = result
    ordered = sorted(best.values(), key=lambda item: (item.rank or 0, item.url))
    return ordered[: max(int(top_k), 0)]
