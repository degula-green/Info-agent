"""Rendering fallback for pages that static fetching cannot read.

Chromium and Playwright live in a separate container (Crawl4AI), not in the
Agent process: a headless browser is a large, frequently-updated attack
surface, and keeping it out means a rendering outage degrades research rather
than taking the service down.

The official image secures every endpoint behind a bearer token, so the token
is configuration, not an option: without it the sidecar answers only inside its
own container and every render fails. The documented single-page call is
``POST /md`` with ``{"url": ...}``, which returns just the Markdown.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_USER_AGENT = "info-agent/0.1 (+web.research)"


class RenderError(RuntimeError):
    """Base error; the classification attribute is what the kernel reads."""

    classification = "permanent_error"
    code = "render_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class RenderUnavailable(RenderError):
    """Transport-level failure: the same page may render next time."""

    classification = "retryable_error"
    code = "render_unavailable"


class RenderedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str
    status: int = Field(default=200, ge=0)
    title: str = ""
    markdown: str = ""
    links: list[str] = Field(default_factory=list)
    # Which implementation produced this body. Evidence carries it through, so
    # a reader can tell a self-fetched page from a vendor's extraction.
    fetch_method: str = "crawl4ai"


class Crawl4AIClient:
    """Calls the Crawl4AI sidecar; ``_open`` is the test seam."""

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str = "",
        timeout_seconds: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.api_token = str(api_token or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.user_agent = user_agent

    def render(self, url: str) -> RenderedPage:
        if not self.base_url:
            raise RenderUnavailable("crawl4ai is not configured", code="render_not_configured")
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = urllib.request.Request(
            f"{self.base_url}/md",
            data=json.dumps({"url": str(url)}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._open(request) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status >= 400:
                    raise self._status_error(status)
                payload = response.read()
        except RenderError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._status_error(int(exc.code or 0)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RenderUnavailable(f"render request failed: {exc}") from exc
        return self._parse(url, payload)

    def _open(self, request):
        """Opens the request; tests substitute this to avoid a live socket."""

        return urllib.request.urlopen(request, timeout=self.timeout_seconds)

    @staticmethod
    def _parse(url: str, payload: bytes) -> RenderedPage:
        try:
            body = json.loads(payload.decode("utf-8", errors="replace") or "{}")
        except ValueError as exc:
            raise RenderError("crawl4ai returned invalid JSON", code="render_invalid") from exc
        if not isinstance(body, dict):
            raise RenderError("crawl4ai response is not an object", code="render_invalid")
        links = body.get("links")
        return RenderedPage(
            url=str(body.get("url") or url),
            final_url=str(body.get("final_url") or body.get("url") or url),
            status=int(body.get("status") or 200),
            title=str(body.get("title") or ""),
            markdown=str(body.get("markdown") or ""),
            links=[str(item) for item in links] if isinstance(links, list) else [],
        )

    @staticmethod
    def _status_error(status: int) -> RenderError:
        if status == 429 or status >= 500:
            return RenderUnavailable(f"render failed ({status})", code="render_http_error")
        return RenderError(f"render rejected ({status})", code="render_http_error")
