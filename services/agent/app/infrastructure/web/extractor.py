"""Readable-text extraction shared by the legacy extract step and web.research.

Conservative on purpose: no layout guessing, no scripts, no stylesheets. The
worst outcome for a research task is not "ugly text", it is silently dropping
the sentence the answer depended on, so the parser keeps everything a human
would see in the page body.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin

# Tags whose text is markup rather than content.
SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
# Tags that end a line of prose; without them the whole page becomes one line.
BREAK_TAGS = frozenset(
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


class ReadableTextExtractor(HTMLParser):
    """Collects title, prose and links from one document."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
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
        if tag in BREAK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in BREAK_TAGS:
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


def clip(text: str, limit: int) -> str:
    """Caps the body handed to a model without cutting mid-word."""

    body = str(text or "")
    limit = max(int(limit), 1)
    if len(body) <= limit:
        return body
    window = body[:limit]
    cut = window.rfind(" ")
    return (window[:cut] if cut > limit // 2 else window).rstrip()


def extract_document(
    document: str, *, url: str | None = None
) -> tuple[str, str, list[str]]:
    """Returns ``(title, text, links)`` for one fetched document."""

    extractor = ReadableTextExtractor(base_url=url)
    extractor.feed(str(document or ""))
    extractor.close()
    title = normalize_text(" ".join(extractor.title_parts))
    text = normalize_text(" ".join(extractor.text_parts))
    if text and title and text.startswith(title):
        text = text[len(title) :].lstrip()
    return title, text, list(extractor.links)
