from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.domain.rag import SearchResult


@dataclass(frozen=True)
class ContextBundle:
    text: str
    citations: list[dict[str, object]]


def assemble_context(
    results: list[SearchResult],
    *,
    max_chunks: int | None = None,
    max_tokens: int | None = None,
) -> ContextBundle:
    limit = max_chunks or settings.qa_max_chunks
    token_limit = max_tokens or settings.qa_max_context_tokens
    groups: dict[str, dict[str, Any]] = {}
    used = 0
    for result in results[:limit]:
        source = result.source
        if not bool(source.get("rag_eligible", True)):
            continue
        text = result.content.strip()
        if not text:
            continue
        clipped = _clip(text, max(1, token_limit - used))
        if not clipped:
            break
        used += _estimate_tokens(clipped)
        group = _source_group(result, clipped)
        current = groups.setdefault(group["source_id"], group)
        current["fragments"].append(clipped)
        current["evidence_chunk_ids"].append(result.chunk_id)
    pieces: list[str] = []
    citations: list[dict[str, object]] = []
    for rank, group in enumerate(groups.values(), start=1):
        fragments = list(dict.fromkeys(group.pop("fragments")))
        group["evidence_chunk_ids"] = list(dict.fromkeys(group["evidence_chunk_ids"]))
        pieces.append(f"[资料 {rank}] {group['display_name']}\n" + "\n---\n".join(fragments))
        citations.append({"rank": rank, **group})
    return ContextBundle("\n\n".join(pieces), citations)


def _source_group(result: SearchResult, summary: str) -> dict[str, Any]:
    source = result.source
    resource_type = str(source.get("resource_type") or "message")
    resource_id = str(source.get("resource_id") or result.knowledge_item_id or result.chunk_id)
    if resource_type == "attachment":
        display_name = str(source.get("file_name") or source.get("title") or "附件")
        return {
            "source_id": f"attachment:{resource_id}",
            "source_kind": "document",
            "display_name": display_name,
            "file_name": display_name,
            "attachment_id": resource_id,
            "knowledge_item_id": result.knowledge_item_id,
            "knowledge_base_id": source.get("knowledge_base_id"),
            "scope_key": source.get("scope_key"),
            "content_version": source.get("content_version"),
            "acl_version": source.get("acl_version"),
            "conversation_id": source.get("source_conversation_id"),
            "message_id": source.get("message_id"),
            "evidence_chunk_ids": [],
            "fragments": [],
        }
    compact = " ".join(summary.split())
    message_summary = compact[:80] + ("..." if len(compact) > 80 else "")
    return {
        "source_id": f"message:{resource_id}",
        "source_kind": "message",
        "display_name": f"消息：{message_summary}",
        "message_summary": message_summary,
        "message_id": source.get("message_id") or resource_id,
        "conversation_id": source.get("source_conversation_id"),
        "knowledge_item_id": result.knowledge_item_id,
        "knowledge_base_id": source.get("knowledge_base_id"),
        "scope_key": source.get("scope_key"),
        "content_version": source.get("content_version"),
        "acl_version": source.get("acl_version"),
        "evidence_chunk_ids": [],
        "fragments": [],
    }


def _estimate_tokens(value: str) -> int:
    return len(value.split()) + sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _clip(value: str, budget: int) -> str:
    if _estimate_tokens(value) <= budget:
        return value
    return "".join(list(value)[: max(1, budget)]).rstrip()
