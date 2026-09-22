from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.domain.models import SearchResult


@dataclass(frozen=True)
class ContextBundle:
    text: str
    citations: list[dict[str, object]]


def assemble_context(results: list[SearchResult], *, max_chunks: int | None = None, max_tokens: int | None = None) -> ContextBundle:
    """Use documents/messages as citations while retaining chunks as evidence."""
    limit = max_chunks or settings.qa_max_chunks
    token_limit = max_tokens or settings.qa_max_context_tokens
    document_names = {
        str(result.attachment_id): str(result.source.get("file_name"))
        for result in results
        if result.attachment_id and str(result.source.get("file_name") or "").strip()
    }
    groups: dict[str, dict[str, Any]] = {}
    used = 0
    for result in results[:limit]:
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
        group = _source_group(result, document_names, clipped)
        current = groups.setdefault(group["source_id"], group)
        current["fragments"].append(clipped)
        current["evidence_chunk_ids"].append(result.chunk_id)
        relation = result.source.get("evidence_relation")
        if relation:
            current.setdefault("evidence_relations", []).append(relation)
        tree_path = result.source.get("tree_path")
        if tree_path:
            current.setdefault("tree_paths", []).append(tree_path)
        fact_id = result.source.get("fact_id")
        if fact_id:
            current.setdefault("fact_ids", []).append(fact_id)

    pieces: list[str] = []
    citations: list[dict[str, object]] = []
    for rank, group in enumerate(groups.values(), start=1):
        fragments = list(dict.fromkeys(group.pop("fragments")))
        group["evidence_chunk_ids"] = list(dict.fromkeys(group["evidence_chunk_ids"]))
        group["evidence_relations"] = list(dict.fromkeys(group.get("evidence_relations", [])))
        paths: list[object] = []
        path_keys: set[str] = set()
        for path in group.get("tree_paths", []):
            key = repr(path)
            if key not in path_keys:
                path_keys.add(key)
                paths.append(path)
        group["tree_paths"] = paths
        group["fact_ids"] = list(dict.fromkeys(group.get("fact_ids", [])))
        pieces.append(f"[资料 {rank}] {group['display_name']}\n" + "\n---\n".join(fragments))
        citations.append({"rank": rank, **group})
    return ContextBundle("\n\n".join(pieces), citations)


def _source_group(result: SearchResult, document_names: dict[str, str], summary: str) -> dict[str, Any]:
    source = result.source
    attachment_id = str(result.attachment_id or "")
    if attachment_id and attachment_id in document_names:
        return {
            "source_id": f"attachment:{attachment_id}", "source_kind": "document",
            "display_name": document_names[attachment_id], "file_name": document_names[attachment_id],
            "attachment_id": attachment_id, "knowledge_item_id": result.knowledge_item_id,
            "knowledge_base_id": source.get("knowledge_base_id"), "organization_id": source.get("organization_id"),
            "content_version": source.get("content_version"), "acl_version": source.get("auth_acl_version"),
            "conversation_id": source.get("conversation_id") or source.get("conversation_group_id"),
            "platform": source.get("platform"), "message_id": source.get("message_id") or source.get("source_resource_id"),
            "evidence_chunk_ids": [], "evidence_relations": [], "tree_paths": [], "fact_ids": [], "fragments": [],
        }
    message_id = str(source.get("message_id") or source.get("source_resource_id") or result.knowledge_item_id or result.chunk_id)
    message_summary = _summary(summary)
    return {
        "source_id": f"message:{message_id}", "source_kind": "message",
        "display_name": f"飞书消息：{message_summary}", "message_summary": message_summary,
        "message_id": message_id, "conversation_id": source.get("conversation_id") or source.get("conversation_group_id") or source.get("conversation_key"),
        "platform": source.get("platform") or "feishu", "knowledge_item_id": result.knowledge_item_id,
        "knowledge_base_id": source.get("knowledge_base_id"), "organization_id": source.get("organization_id"),
        "content_version": source.get("content_version"), "acl_version": source.get("auth_acl_version"),
        "evidence_chunk_ids": [], "evidence_relations": [], "tree_paths": [], "fact_ids": [], "fragments": [],
    }


def _summary(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:80] + ("..." if len(compact) > 80 else "")


def _estimate_tokens(value: str) -> int:
    return len(value.split()) + sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _clip(value: str, budget: int) -> str:
    if _estimate_tokens(value) <= budget:
        return value
    units = list(value)
    return "".join(units[: max(1, budget)]).rstrip()
