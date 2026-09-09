from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.domain.models import SearchResult


@dataclass(frozen=True)
class ContextBundle:
    text: str
    citations: list[dict[str, object]]


def assemble_context(results: list[SearchResult], *, max_chunks: int | None = None, max_tokens: int | None = None) -> ContextBundle:
    limit = max_chunks or settings.qa_max_chunks
    token_limit = max_tokens or settings.qa_max_context_tokens
    pieces: list[str] = []
    citations: list[dict[str, object]] = []
    used = 0
    for rank, result in enumerate(results[:limit], start=1):
        if not bool(result.source.get("rag_eligible", True)):
            continue
        text = result.content.strip()
        if not text:
            continue
        budget = max(1, token_limit - used)
        clipped = _clip(text, budget)
        if not clipped:
            break
        used += _estimate_tokens(clipped)
        label = result.source.get("title") or result.source.get("file_name") or result.knowledge_item_id or result.chunk_id
        pieces.append(f"[资料 {rank}] {label}\n{clipped}")
        citations.append({
            "knowledge_item_id": result.knowledge_item_id,
            "attachment_id": result.attachment_id,
            "content_version": result.source.get("content_version"),
            "acl_version": result.source.get("auth_acl_version"),
            "es_chunk_id": result.chunk_id,
            "rank": rank,
            "score": result.score,
            "quote_text": clipped,
        })
    return ContextBundle("\n\n".join(pieces), citations)


def _estimate_tokens(value: str) -> int:
    return len(value.split()) + sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _clip(value: str, budget: int) -> str:
    if _estimate_tokens(value) <= budget:
        return value
    units = list(value)
    return "".join(units[: max(1, budget)]).rstrip()
