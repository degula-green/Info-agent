"""One reading path per URL: static first, rendering only when it earns it.

A headless browser is slow and costly, so it is the exception rather than the
default. The rules here are the ones in the design document's fallback table:
static wins when it produces a real body, rendering covers JavaScript pages,
anti-bot answers and short stubs, and 404/410 or a blocked address is final --
asking a renderer about a page that does not exist only wastes the budget.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.web.crawl4ai_client import RenderError
from app.infrastructure.web.extractor import extract_document
from app.providers.page_fetcher import PageFetchError

DEFAULT_MIN_TEXT_CHARS = 500
_DOCUMENT_TYPES = frozenset({"application/pdf"})
_MEDIA_PREFIXES = ("audio/", "video/")


class ContentReadError(RuntimeError):
    """Base error; the classification attribute is what the kernel reads."""

    classification = "permanent_error"
    code = "content_read_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ContentUnavailable(ContentReadError):
    """Transient failure: the same URL may be readable on the next attempt."""

    classification = "retryable_error"
    code = "content_unavailable"


class UnsupportedDocument(ContentReadError):
    """A page this capability must not handle (PDF, audio, video)."""

    code = "unsupported_document"


class ReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str
    status: int = Field(default=200, ge=0)
    content_type: str = "text/html"
    title: str = ""
    text: str = ""
    links: list[str] = Field(default_factory=list)
    fetch_method: str = "static"
    truncated: bool = False
    # Degradations worth telling the caller about: a page that was read but only
    # from a short static body, because rendering failed. They are not errors --
    # the page was read -- but they explain a thin evidence record.
    notes: list[str] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContentReader:
    """Static fetch, with an optional rendering fallback."""

    def __init__(
        self,
        fetcher,
        *,
        renderer=None,
        min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    ) -> None:
        self.fetcher = fetcher
        self.renderer = renderer
        self.min_text_chars = max(int(min_text_chars), 1)

    def read(self, url: str) -> ReadResult:
        document = None
        static_error: PageFetchError | None = None
        try:
            document = self.fetcher.fetch(url)
        except PageFetchError as exc:
            static_error = exc

        if document is not None:
            self._reject_unsupported(document.content_type, url)
            title, text, links = extract_document(document.content, url=document.final_url or url)
            if len(text) >= self.min_text_chars or self.renderer is None:
                return ReadResult(
                    url=url,
                    final_url=document.final_url or url,
                    status=int(document.status),
                    content_type=document.content_type,
                    title=title,
                    text=text,
                    links=links,
                    fetch_method="static",
                    truncated=bool(document.truncated),
                    retrieved_at=document.fetched_at,
                )
            try:
                return self._render(url)
            except RenderError as exc:
                # Half a page beats no page: keep what the static fetch got, but
                # say why it is only half a page.
                return ReadResult(
                    url=url,
                    final_url=document.final_url or url,
                    status=int(document.status),
                    content_type=document.content_type,
                    title=title,
                    text=text,
                    links=links,
                    fetch_method="static",
                    truncated=bool(document.truncated),
                    notes=[f"static body was short and rendering failed: {exc}"],
                    retrieved_at=document.fetched_at,
                )

        assert static_error is not None
        self._reject_unsupported(str(getattr(static_error, "content_type", "") or ""), url)
        if not self._worth_rendering(static_error):
            raise ContentReadError(
                f"cannot read {url}: {static_error}", code=static_error.code
            ) from static_error
        try:
            return self._render(url)
        except RenderError as exc:
            # Report both failures. Seeing only "renderer failed" here once cost
            # a real debugging round: the actionable half was the static error.
            raise ContentUnavailable(
                f"cannot read {url}: {static_error}; renderer also failed: {exc}",
                code=exc.code,
            ) from exc

    def _render(self, url: str) -> ReadResult:
        if self.renderer is None:
            raise ContentUnavailable(
                f"no renderer available for {url}", code="renderer_not_configured"
            )
        page = self.renderer.render(url)
        text = str(page.markdown or "").strip()
        return ReadResult(
            url=url,
            final_url=page.final_url or url,
            status=int(page.status),
            content_type="text/markdown",
            title=str(page.title or ""),
            text=text,
            links=list(page.links or []),
            fetch_method=page.fetch_method,
        )

    @staticmethod
    def _reject_unsupported(content_type: str, url: str) -> None:
        """PDF and media are other components' job, not this capability's."""

        kind = str(content_type or "").split(";")[0].strip().lower()
        if not kind:
            return
        if kind in _DOCUMENT_TYPES:
            raise UnsupportedDocument(
                f"{url} is a document for the RAG pipeline, not a web page",
                code="document_not_web_page",
            )
        if kind.startswith(_MEDIA_PREFIXES):
            raise UnsupportedDocument(f"{url} is media, which is not supported", code="unsupported_media")

    @staticmethod
    def _worth_rendering(error: PageFetchError) -> bool:
        """Whether a browser might succeed where a plain GET did not."""

        if error.classification == "retryable_error":
            return True
        status = getattr(error, "status", None)
        if status in (404, 410):
            return False
        return error.code not in {
            "unsupported_scheme",
            "invalid_url",
            "blocked_address",
            "unsupported_content_type",
            "page_not_found",
        }
