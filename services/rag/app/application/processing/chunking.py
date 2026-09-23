from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.config import settings
from app.domain.models import AttachmentContext, CanonicalBlock, ChunkRecord, ParsedDocument


@dataclass(frozen=True)
class _LogicalBlock:
    text: str
    block_type: str
    order: int
    page_number: int | None
    heading_path: tuple[str, ...]
    metadata: dict[str, Any]


def build_chunks(parsed: ParsedDocument, context: AttachmentContext) -> list[ChunkRecord]:
    """Create deterministic, type-aware chunks from canonical blocks."""
    logical = _logical_blocks(parsed.blocks, context)
    if not logical and context.part_kind == "attachment_metadata":
        label = context.title or context.file_name
        logical = [_LogicalBlock(label, "metadata", 0, None, (), {"metadata_only": True})]
    pieces: list[tuple[_LogicalBlock, str, int]] = []
    for block in logical:
        limit = settings.chunk_max_tokens
        if block.block_type in {"table", "equation", "image", "code"}:
            limit = max(limit, 1024 if block.block_type == "table" else limit)
        if block.block_type == "table":
            windows = _split_table(block.text, limit)
        else:
            windows = _split_tokens(block.text, limit, settings.chunk_overlap_tokens if block.block_type in {"text", "list", "code"} else 0)
        if not windows:
            continue
        for part_index, value in enumerate(windows):
            pieces.append((block, value, part_index))
    if not pieces:
        return []

    count = len(pieces)
    records: list[ChunkRecord] = []
    for index, (block, text, part_index) in enumerate(pieces):
        content = _with_context(block, text)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        is_attachment = context.part_kind in {"attachment_content", "attachment_metadata"}
        protected = context.content_access_required or context.part_kind == "knowledge_original"
        record_part_kind = "knowledge_original" if protected and not is_attachment else context.part_kind
        # The ID is versioned by business content and chunk position, not by
        # the text hash. Reprocessing the same version therefore replaces old
        # chunks instead of leaving stale documents behind in ES.
        stable = "|".join(
            [
                context.knowledge_item_id or context.attachment_id,
                str(context.content_version),
                record_part_kind,
                context.attachment_id if is_attachment else "",
                str(context.attachment_content_version or 0) if is_attachment else "0",
                settings.chunking_version,
                str(index),
            ]
        )
        chunk_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        if record_part_kind == "knowledge_original":
            auth_type, auth_part, auth_id, auth_key = "knowledge_item", "original", context.knowledge_item_id or context.attachment_id, f"knowledge_original:{context.knowledge_item_id or context.attachment_id}"
        elif is_attachment:
            auth_part = "metadata" if context.part_kind == "attachment_metadata" else "content"
            auth_key_prefix = "attachment_meta" if auth_part == "metadata" else "attachment_content"
            auth_type, auth_id, auth_key = "attachment", context.attachment_id, f"{auth_key_prefix}:{context.attachment_id}"
        else:
            auth_type, auth_part, auth_id, auth_key = "knowledge_item", "display", context.knowledge_item_id or context.attachment_id, f"knowledge_item:{context.knowledge_item_id or context.attachment_id}"
        source_locator = dict(context.source_locator)
        if is_attachment:
            source_locator.setdefault("file_name", context.file_name)
            source_locator.setdefault("mime_type", context.mime_type)
        source_locator.setdefault("part_kind", context.part_kind)
        if block.page_number is not None:
            source_locator.setdefault("page_number", block.page_number)
        source_locator.setdefault("paragraph_index", block.order)
        if block.metadata.get("slide_number") is not None:
            source_locator.setdefault("slide_number", block.metadata["slide_number"])
        if block.metadata.get("sheet_name") is not None:
            source_locator.setdefault("sheet_name", block.metadata["sheet_name"])
        records.append(
            ChunkRecord(
                chunk_id=chunk_id,
                chunk_index=index,
                chunk_count=count,
                chunking_version=settings.chunking_version,
                knowledge_item_id=context.knowledge_item_id or context.attachment_id,
                attachment_id=context.attachment_id if is_attachment else None,
                knowledge_base_id=context.knowledge_base_id,
                content_version=context.content_version,
                attachment_content_version=(context.attachment_content_version or context.content_version) if is_attachment else None,
                auth_acl_version=context.acl_version,
                mapping_version="v1",
                auth_resource_type=auth_type,
                auth_resource_part=auth_part,
                auth_resource_id=auth_id,
                auth_object_key=auth_key,
                knowledge_scope=context.knowledge_scope,
                access_scope=context.access_scope,
                organization_id=context.organization_id,
                owner_user_id=context.owner_user_id,
                conversation_group_id=context.conversation_group_id,
                part_kind=record_part_kind,
                content_access_required=protected,
                title=" / ".join(block.heading_path) or context.title,
                content=content,
                content_hash=content_hash,
                content_visibility="protected" if protected else "display",
                embedding_model=None,
                vectorized=False,
                rag_eligible=bool(content.strip()) and context.part_kind != "attachment_metadata",
                file_name=context.file_name if is_attachment else None,
                mime_type=context.mime_type if is_attachment else None,
                size_bytes=context.size_bytes,
                source_locator=source_locator,
                conversation_ingestion_id=context.conversation_ingestion_id,
                external_conversation_id=context.external_conversation_id,
                message_id=context.message_id,
                external_message_id=context.external_message_id,
                sender_identity_id=context.sender_identity_id,
                sender_display_name=context.sender_display_name,
                sent_at=context.sent_at,
                sensitivity=context.sensitivity,
                lifecycle_status=context.lifecycle_status,
                chunk_type=block.block_type,
            )
        )
    return records


def _logical_blocks(blocks: Iterable[CanonicalBlock], context: AttachmentContext) -> list[_LogicalBlock]:
    output: list[_LogicalBlock] = []
    pending_heading: tuple[str, ...] = ()
    for block in sorted(blocks, key=lambda item: item.order):
        heading = block.heading_path or pending_heading
        if block.type == "title":
            pending_heading = heading or (block.text,)
            continue
        text = block.searchable_text.strip()
        if not text and block.type != "image":
            continue
        metadata = dict(block.metadata)
        # Preserve table/worksheet metadata supplied by parsers and fixtures.
        if context.file_name.lower().endswith((".csv", ".tsv", ".xlsx")):
            metadata.setdefault("sheet_name", context.title or context.file_name)
        output.append(_LogicalBlock(text, block.type, block.order, block.page_number, heading, metadata))
    return output


def _with_context(block: _LogicalBlock, text: str) -> str:
    prefix = " / ".join(block.heading_path)
    if block.block_type == "code":
        language = str(block.metadata.get("language") or "")
        prefix = f"{prefix} code {language}".strip()
    elif block.block_type == "table":
        prefix = f"{prefix} table".strip()
    elif block.block_type == "equation":
        prefix = f"{prefix} equation".strip()
    return f"{prefix}\n{text}".strip() if prefix else text


def _split_tokens(value: str, limit: int, overlap: int) -> list[str]:
    units = _token_units(value)
    if not units:
        return []
    limit = max(1, limit)
    overlap = max(0, min(overlap, limit - 1))
    if len(units) <= limit:
        return ["".join(units).strip()]
    result: list[str] = []
    start = 0
    while start < len(units):
        end = min(len(units), start + limit)
        part = "".join(units[start:end]).strip()
        if part:
            result.append(part)
        if end >= len(units):
            break
        start = max(start + 1, end - overlap)
    return result


def _split_table(value: str, limit: int) -> list[str]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) <= 2 or len(_token_units(value)) <= limit:
        return [value.strip()] if value.strip() else []
    header = lines[:2]
    output: list[str] = []
    rows: list[str] = []
    for line in lines[2:]:
        candidate = "\n".join(header + rows + [line])
        if rows and len(_token_units(candidate)) > limit:
            output.append("\n".join(header + rows).strip())
            rows = [line]
        else:
            rows.append(line)
    if rows:
        output.append("\n".join(header + rows).strip())
    return output


def _token_units(value: str) -> list[str]:
    # Keep CJK characters and ASCII words searchable while retaining spaces and
    # punctuation enough to reconstruct readable text.
    return re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|\s+|[^\w\s]", value, flags=re.UNICODE)


def json_block(value: Any, context: AttachmentContext) -> CanonicalBlock:
    """Build a structural JSON block for callers that bypass LocalParser."""
    return CanonicalBlock(
        page_number=None,
        order=0,
        type="text",
        text=json.dumps(value, ensure_ascii=False, indent=2),
        heading_path=(context.title or context.file_name,),
        metadata={"format": "json"},
    )
