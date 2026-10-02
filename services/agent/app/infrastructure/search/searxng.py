"""SearXNG-backed search discovery.

SearXNG runs as an internal container with JSON output enabled (its default
HTML endpoint is not machine-readable) and is never exposed publicly. The
client uses the standard library so the Agent process gains no new dependency
for the one HTTP call it makes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.infrastructure.search.client import (
    SearchError,
    SearchResult,
    SearchUnavailable,
)
from app.infrastructure.web.url_tools import with_site_filter

DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_CATEGORIES = "general"
DEFAULT_USER_AGENT = "info-agent/0.1 (+web.research)"


class SearxngSearchProvider:
    """Calls SearXNG's JSON endpoint; ``_open`` is the test seam."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        categories: str = DEFAULT_CATEGORIES,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.categories = str(categories or DEFAULT_CATEGORIES)
        self.user_agent = user_agent

    def search(
        self,
        query: str,
        *,
        top_k: int,
        include_domains: list[str],
        language: str = DEFAULT_LANGUAGE,
    ) -> list[SearchResult]:
        if not self.base_url:
            raise SearchUnavailable("searxng is not configured", code="search_not_configured")
        text = str(query or "").strip()
        if not text:
            return []
        scoped = with_site_filter(text, list(include_domains or []))
        params = urllib.parse.urlencode(
            {
                "q": scoped,
                "format": "json",
                "language": str(language or DEFAULT_LANGUAGE),
                "categories": self.categories,
            }
        )
        request = urllib.request.Request(
            f"{self.base_url}/search?{params}",
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            method="GET",
        )
        try:
            with self._open(request) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status >= 400:
                    raise self._status_error(status)
                payload = response.read()
        except SearchError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._status_error(int(exc.code or 0)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SearchUnavailable(f"search request failed: {exc}") from exc
        return self._parse(payload, top_k=top_k)

    def _open(self, request):
        """Opens the request; tests substitute this to avoid a live socket."""

        return urllib.request.urlopen(request, timeout=self.timeout_seconds)

    def _parse(self, payload: bytes, *, top_k: int) -> list[SearchResult]:
        try:
            body = json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except ValueError as exc:
            raise SearchError("searxng returned invalid JSON", code="search_invalid") from exc
        raw_results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(raw_results, list):
            raise SearchError("searxng response has no results list", code="search_invalid")
        results: list[SearchResult] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_results, start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("content") or item.get("snippet") or ""),
                    rank=index,
                    provider="searxng",
                    published_at=item.get("publishedDate") or None,
                )
            )
            if len(results) >= max(int(top_k), 0):
                break
        return results

    @staticmethod
    def _status_error(status: int) -> SearchError:
        """429 and 5xx are worth retrying; the rest will not change by asking."""

        if status == 429 or status >= 500:
            return SearchUnavailable(f"search failed ({status})", code="search_http_error")
        return SearchError(f"search rejected ({status})", code="search_http_error")
