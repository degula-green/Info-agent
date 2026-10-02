"""Tavily-backed search discovery.

Tavily is a hosted service, so this provider trades control for reach: it
covers engines and regions a self-hosted metasearch cannot, and it takes the
query out of our network. It is selected by configuration, not hard-wired, so
the self-hosted provider stays one setting away.

The API takes the domain restriction natively (``include_domains``), which is
why this provider does not paste ``site:`` into the query the way the SearXNG
client has to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.infrastructure.search.client import (
    SearchError,
    SearchResult,
    SearchUnavailable,
)

DEFAULT_BASE_URL = "https://api.tavily.com"
DEFAULT_USER_AGENT = "info-agent/0.1 (+web.research)"
SEARCH_DEPTHS = frozenset({"basic", "advanced", "fast", "ultra-fast"})


class TavilySearchProvider:
    """Calls Tavily's /search; ``_open`` is the test seam."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str = "",
        search_depth: str = "basic",
        include_raw_content: bool = False,
        timeout_seconds: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = str(api_key or "").strip()
        depth = str(search_depth or "basic").strip().lower()
        self.search_depth = depth if depth in SEARCH_DEPTHS else "basic"
        self.include_raw_content = bool(include_raw_content)
        self.timeout_seconds = float(timeout_seconds)
        self.user_agent = user_agent

    @property
    def provider_name(self) -> str:
        return "tavily"

    def search(
        self,
        query: str,
        *,
        top_k: int,
        include_domains: list[str],
        language: str = "",
    ) -> list[SearchResult]:
        text = str(query or "").strip()
        if not text:
            return []
        payload: dict = {
            "query": text,
            "max_results": max(int(top_k), 1),
            "search_depth": self.search_depth,
            "include_raw_content": self.include_raw_content,
        }
        if include_domains:
            payload["include_domains"] = list(include_domains)
        request = self._request("/search", payload)
        body = self._send(request)
        return self._parse(body, top_k=top_k)

    def _request(self, path: str, payload: dict) -> urllib.request.Request:
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            # Documented no-account mode; Search and Extract allow it.
            headers["X-Tavily-Access-Mode"] = "keyless"
        return urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _send(self, request: urllib.request.Request) -> bytes:
        try:
            with self._open(request) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status >= 400:
                    raise self._status_error(status)
                return response.read()
        except SearchError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._status_error(int(exc.code or 0)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SearchUnavailable(f"tavily request failed: {exc}") from exc

    def _open(self, request):
        """Opens the request; tests substitute this to avoid a live socket."""

        return urllib.request.urlopen(request, timeout=self.timeout_seconds)

    @staticmethod
    def _parse(payload: bytes, *, top_k: int) -> list[SearchResult]:
        try:
            body = json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except ValueError as exc:
            raise SearchError("tavily returned invalid JSON", code="search_invalid") from exc
        raw_results = body.get("results") if isinstance(body, dict) else None
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise SearchError("tavily response has no results list", code="search_invalid")
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
                    # Search content is a ranked excerpt; only a fetched body
                    # may become evidence, so it is carried as a snippet here.
                    snippet=str(item.get("content") or item.get("snippet") or ""),
                    rank=index,
                    provider="tavily",
                    published_at=item.get("published_date") or item.get("publishedDate"),
                )
            )
            if len(results) >= max(int(top_k), 0):
                break
        return results

    @staticmethod
    def _status_error(status: int) -> SearchError:
        if status == 429 or status >= 500:
            return SearchUnavailable(f"tavily search failed ({status})", code="search_http_error")
        return SearchError(f"tavily search rejected ({status})", code="search_http_error")
