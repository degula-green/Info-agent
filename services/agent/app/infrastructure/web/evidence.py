"""The Evidence contract produced by ``web.research``.

Answer composition is only as trustworthy as this record: it is the sole set
of ids a model is allowed to cite, and it carries the hash, the retrieval time
and the method that produced the text, so a reader can tell where a sentence
came from without refetching the page.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from app.infrastructure.web.extractor import clip, normalize_text

EVIDENCE_VERSION = 2
QUOTE_CHARS = 280
# Navigation chrome and headings are short by nature ("Theme Auto Light Dark",
# "Table of Contents"). Quoting one of those tells a reader nothing, so a
# substantive sentence wins whenever the page has one.
MIN_QUOTE_SENTENCE_CHARS = 30
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\.])\s*")


def content_hash(text: str) -> str:
    """Hash of the normalized body: the same page hashes the same twice."""

    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def select_quote(text: str, question: str | None = None) -> str:
    """The sentence most likely to answer the question, else the opening.

    The quote is what a reader checks first, so an off-topic opening line is
    worse than a shorter, relevant one.
    """

    body = normalize_text(text)
    if not body:
        return ""
    # Split by line first: navigation and headings arrive as separate lines
    # without any sentence punctuation, and a paragraph that spans several
    # lines should still be quotable sentence by sentence.
    sentences: list[str] = []
    for line in body.splitlines():
        for item in SENTENCE_SPLIT.split(line):
            cleaned = item.strip()
            if cleaned:
                sentences.append(cleaned)
    if not sentences:
        return clip(body, QUOTE_CHARS)
    substantive = [item for item in sentences if len(item) >= MIN_QUOTE_SENTENCE_CHARS]
    candidates = substantive or sentences
    terms = {
        term.lower()
        for term in re.split(r"[\s,，。、;；:：?？!！]+", str(question or ""))
        if len(term) >= 2
    }
    best = candidates[0]
    best_score = -1
    for index, sentence in enumerate(candidates):
        lowered = sentence.lower()
        score = sum(1 for term in terms if term in lowered)
        # Earlier sentences win ties: pages lead with their point.
        score -= index * 0.01
        if score > best_score:
            best_score = score
            best = sentence
    return clip(best, QUOTE_CHARS)


def build_evidence(
    *,
    url: str | None,
    title: str,
    text: str,
    quote: str | None = None,
    fetch_method: str = "static",
    question: str | None = None,
    retrieved_at: datetime | None = None,
    max_chars: int | None = None,
) -> dict:
    """One provenance record per page.

    The id is derived from the URL, the body hash and the quote instead of a
    fresh uuid, so a retry of the same step produces the same id and the
    evidence table stays idempotent.
    """

    body = normalize_text(text)
    if max_chars is not None:
        body = clip(body, max_chars)
    selected = clip(quote, QUOTE_CHARS) if quote else select_quote(body, question)
    digest = content_hash(body)
    evidence_id = "ev-" + hashlib.sha256(
        f"{url or ''}|{digest}|{selected}".encode("utf-8")
    ).hexdigest()[:16]
    stamp = retrieved_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return {
        "evidence_id": evidence_id,
        "source_type": "web" if url else "document",
        "title": title or url or "document",
        "url": url,
        "quote": selected,
        "text": body,
        "content_hash": digest,
        "retrieved_at": stamp.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "fetch_method": fetch_method,
        "version": EVIDENCE_VERSION,
    }


class EvidenceBuilder:
    """Configured builder: page budget and quote length are policy, not taste."""

    def __init__(self, *, page_chars: int = 8000, quote_chars: int = QUOTE_CHARS) -> None:
        self.page_chars = max(int(page_chars), 1)
        self.quote_chars = max(int(quote_chars), 1)

    def build(
        self,
        *,
        url: str | None,
        title: str,
        text: str,
        fetch_method: str = "static",
        question: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> dict:
        record = build_evidence(
            url=url,
            title=title,
            text=text,
            fetch_method=fetch_method,
            question=question,
            retrieved_at=retrieved_at,
            max_chars=self.page_chars,
        )
        record["quote"] = clip(record["quote"], self.quote_chars)
        return record
