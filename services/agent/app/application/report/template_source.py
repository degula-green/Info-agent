"""Find the .docx template a weekly report must follow.

Two sources, chosen by the instruction:

* "按我上传的模板" (or any wording that points at the attachment) uses only the
  .docx uploaded with this turn;
* otherwise the collected documents are searched by file name first, then by
  content. An attached .docx is still used when that search finds nothing, so a
  user who attached a template is never told there is no template.

Multiple distinct candidates are returned as a selection instead of being
guessed at: the user asked for a specific format, so picking one silently would
be the wrong kind of helpful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from app.infrastructure.agent_attachments import DOCX_MIME

UPLOADED_MARKERS = (
    "我上传的模板",
    "上传的模板",
    "我上传的",
    "上传的附件",
    "附件里的模板",
    "附件的模板",
    "我发的模板",
    "这个模板",
    "这份模板",
    "该模板",
)

DEFAULT_SEARCH_TERMS = ("周报模板", "周报", "模板")
_QUOTED = re.compile(r"[「“\"']([^」”\"']{2,40})[」”\"']")


class TemplateSearchClient(Protocol):
    def search_attachments(
        self,
        *,
        owner_user_id: str,
        name: str,
        limit: int = 50,
        request_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        ...


def is_docx(mime_type: str, file_name: str = "") -> bool:
    if str(mime_type or "").strip().lower() == DOCX_MIME:
        return True
    return str(file_name or "").strip().lower().endswith(".docx")


@dataclass(frozen=True)
class TemplateCandidate:
    origin: str  # "uploaded" | "collected"
    attachment_id: str
    file_name: str
    size_bytes: int = 0
    created_at: str = ""
    content_hash: str = ""
    object_name: str = ""

    @property
    def dedupe_key(self) -> tuple[str, str, int]:
        # Identical bytes are the same template however the forwarded copy was
        # renamed ("周报模板(1).docx"); the name only matters when the store has
        # no hash to compare, so a genuinely different layout stays separate.
        digest = str(self.content_hash or "").strip().lower()
        if digest:
            return ("", digest, self.size_bytes)
        return (self.file_name, "", self.size_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "attachment_id": self.attachment_id,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
        }


@dataclass
class TemplateResolution:
    status: str  # "resolved" | "needs_selection" | "not_found"
    candidates: list[TemplateCandidate] = field(default_factory=list)
    chosen: TemplateCandidate | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.chosen is not None


def upload_candidates(metadata: Iterable[dict[str, Any]]) -> list[TemplateCandidate]:
    """The .docx files uploaded with this turn, in the order given."""

    out: list[TemplateCandidate] = []
    for item in metadata or []:
        if not isinstance(item, dict):
            continue
        if not is_docx(item.get("mime_type", ""), item.get("file_name", "")):
            continue
        attachment_id = str(item.get("attachment_id") or "").strip()
        if not attachment_id:
            continue
        out.append(
            TemplateCandidate(
                origin="uploaded",
                attachment_id=attachment_id,
                file_name=str(item.get("file_name") or "模板.docx"),
                size_bytes=int(item.get("size_bytes") or 0),
                created_at=str(item.get("uploaded_at") or ""),
                content_hash=str(item.get("content_hash") or ""),
                object_name=str(item.get("minio_object_name") or ""),
            )
        )
    return out


def search_terms(instruction: str) -> list[str]:
    """Query terms for template discovery, most specific first."""

    text = " ".join(str(instruction or "").split())
    terms: list[str] = []
    for quoted in _QUOTED.findall(text):
        cleaned = quoted.strip()
        if cleaned:
            terms.append(cleaned)
    if "周报" in text:
        terms.extend(("周报模板", "周报"))
    if "模板" in text:
        terms.append("模板")
    if not terms:
        terms.extend(DEFAULT_SEARCH_TERMS)
    return list(dict.fromkeys(terms))


def dedupe(candidates: Iterable[TemplateCandidate]) -> list[TemplateCandidate]:
    seen: set[tuple[str, str, int]] = set()
    out: list[TemplateCandidate] = []
    for candidate in candidates:
        key = candidate.dedupe_key
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _collected_candidates(
    knowledge: TemplateSearchClient | None,
    *,
    owner_user_id: str,
    instruction: str,
    request_id: str,
    trace_id: str,
    content_candidates: Callable[[], list[dict[str, Any]]] | None,
) -> list[TemplateCandidate]:
    """File-name search first; content search only when nothing matched."""

    if knowledge is not None:
        for term in search_terms(instruction):
            try:
                response = knowledge.search_attachments(
                    owner_user_id=owner_user_id,
                    name=term,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            except Exception:  # noqa: BLE001 - a degraded search must not fail the turn
                break
            found: list[TemplateCandidate] = []
            for item in response.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if not is_docx(item.get("mime_type", ""), item.get("file_name", "")):
                    continue
                attachment_id = str(item.get("attachment_id") or "").strip()
                if not attachment_id:
                    continue
                found.append(
                    TemplateCandidate(
                        origin="collected",
                        attachment_id=attachment_id,
                        file_name=str(item.get("file_name") or "模板.docx"),
                        size_bytes=int(item.get("size_bytes") or 0),
                        created_at=str(item.get("created_at") or ""),
                        content_hash=str(item.get("content_hash") or ""),
                    )
                )
            if found:
                return dedupe(found)
    if content_candidates is not None:
        try:
            return dedupe(
                TemplateCandidate(
                    origin="collected",
                    attachment_id=str(item.get("attachment_id") or ""),
                    file_name=str(item.get("file_name") or "模板.docx"),
                    size_bytes=int(item.get("size_bytes") or 0),
                    created_at=str(item.get("created_at") or ""),
                    content_hash=str(item.get("content_hash") or ""),
                )
                for item in content_candidates()
                if isinstance(item, dict)
                and is_docx(item.get("mime_type", ""), item.get("file_name", ""))
                and str(item.get("attachment_id") or "").strip()
            )
        except Exception:  # noqa: BLE001 - content search is best effort
            return []
    return []


def resolve_template(
    *,
    instruction: str,
    uploaded: list[dict[str, Any]],
    knowledge: TemplateSearchClient | None,
    owner_user_id: str,
    request_id: str = "",
    trace_id: str = "",
    content_candidates: Callable[[], list[dict[str, Any]]] | None = None,
) -> TemplateResolution:
    uploaded_candidates = upload_candidates(uploaded)
    text = " ".join(str(instruction or "").split())
    wants_uploaded = any(marker in text for marker in UPLOADED_MARKERS)

    if wants_uploaded:
        candidates = dedupe(uploaded_candidates)
    else:
        candidates = _collected_candidates(
            knowledge,
            owner_user_id=owner_user_id,
            instruction=instruction,
            request_id=request_id,
            trace_id=trace_id,
            content_candidates=content_candidates,
        )
        if not candidates:
            candidates = dedupe(uploaded_candidates)

    if not candidates:
        return TemplateResolution(status="not_found")
    if len(candidates) == 1:
        return TemplateResolution(status="resolved", candidates=candidates, chosen=candidates[0])
    return TemplateResolution(status="needs_selection", candidates=candidates)
