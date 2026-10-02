"""Tavily-backed extraction, used as the rendering fallback.

Same role as the Crawl4AI client -- it fills the hole a plain GET could not --
but the fetching happens on Tavily's infrastructure, so two things have to be
handled here that a self-hosted renderer got for free:

* the public-address guard no longer protects anything on its own, so it is
  applied explicitly before the URL leaves this process;
* a per-URL failure arrives inside an HTTP 200 body, so both arrays are read.

The result is marked ``fetch_method="tavily"``: the body came from a vendor's
extraction, and the evidence record has to say so.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.infrastructure.web.crawl4ai_client import (
    RenderError,
    RenderUnavailable,
    RenderedPage,
)
from app.providers.page_fetcher import PageFetchError, assert_public_url

DEFAULT_BASE_URL = "https://api.tavily.com"
DEFAULT_USER_AGENT = "info-agent/0.1 (+web.research)"
EXTRACT_DEPTHS = frozenset({"basic", "advanced"})


class TavilyRenderer:
    """Calls Tavily's /extract; ``_open`` is the test seam."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str = "",
        extract_depth: str = "basic",
        timeout_seconds: float = 30.0,
        allow_private_addresses: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = str(api_key or "").strip()
        depth = str(extract_depth or "basic").strip().lower()
        self.extract_depth = depth if depth in EXTRACT_DEPTHS else "basic"
        self.timeout_seconds = float(timeout_seconds)
        # The operator's fake-IP setting, applied to the pre-flight guard so the
        # new path behaves like the static one instead of rejecting everything.
        self.allow_private_addresses = bool(allow_private_addresses)
        self.user_agent = user_agent

    def render(self, url: str) -> RenderedPage:
        target = str(url or "").strip()
        try:
            assert_public_url(
                target, allow_private_addresses=self.allow_private_addresses
            )
        except PageFetchError as exc:
            # Handing an internal address to a third party is the one failure
            # mode self-hosting made impossible; refuse it here.
            raise RenderError(
                f"refusing to send {target} to tavily: {exc}", code=exc.code
            ) from exc
        request = self._request("/extract", {"urls": [target], "extract_depth": self.extract_depth})
        body = self._send(request)
        return self._parse(target, body)

    def _request(self, path: str, payload: dict) -> urllib.request.Request:
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
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
        except RenderError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._status_error(int(exc.code or 0)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RenderUnavailable(f"tavily request failed: {exc}") from exc

    def _open(self, request):
        """Opens the request; tests substitute this to avoid a live socket."""

        return urllib.request.urlopen(request, timeout=self.timeout_seconds)

    @staticmethod
    def _parse(url: str, payload: bytes) -> RenderedPage:
        try:
            body = json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except ValueError as exc:
            raise RenderError("tavily returned invalid JSON", code="render_invalid") from exc
        if not isinstance(body, dict):
            raise RenderError("tavily response is not an object", code="render_invalid")
        results = body.get("results")
        failed = body.get("failed_results")
        if isinstance(failed, list):
            for item in failed:
                if isinstance(item, dict) and str(item.get("url") or "") == url:
                    # A per-URL failure inside a 200: the vendor could not fetch
                    # it, which is usually transient.
                    raise RenderUnavailable(
                        f"tavily could not extract {url}: {item.get('error') or 'unknown error'}",
                        code="render_failed",
                    )
        if not isinstance(results, list) or not results:
            raise RenderError(f"tavily returned no content for {url}", code="render_empty")
        first = results[0] if isinstance(results[0], dict) else {}
        markdown = str(first.get("raw_content") or "")
        if not markdown.strip():
            raise RenderError(f"tavily returned empty content for {url}", code="render_empty")
        return RenderedPage(
            url=str(first.get("url") or url),
            final_url=str(first.get("url") or url),
            status=200,
            title="",
            markdown=markdown,
            links=[],
            fetch_method="tavily",
        )

    @staticmethod
    def _status_error(status: int) -> RenderError:
        if status == 429 or status >= 500:
            return RenderUnavailable(f"tavily extract failed ({status})", code="render_http_error")
        return RenderError(f"tavily extract rejected ({status})", code="render_http_error")
