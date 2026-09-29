from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.config import settings
from app.domain.models import CanonicalBlock, ParsedDocument
from app.domain.rag import Chunk, ResourceContext


@dataclass(frozen=True)
class _LogicalBlock:
    text: str
    block_type: str
    order: int
    page_number: int | None
    heading_path: tuple[str, ...]
    metadata: dict[str, Any]


def build_variant_chunks(
    parsed: ParsedDocument,
    context: ResourceContext,
    *,
    snapshot_id: str,
    variant: str,
    processing_version: str,
    chunking_version: str,
) -> list[Chunk]:
    logical = _logical_blocks(parsed.blocks, context)
    pieces: list[tuple[_LogicalBlock, str]] = []
    for block in logical:
        limit = settings.chunk_max_tokens
        if block.block_type in {"table", "equation", "image", "code"}:
            limit = max(limit, 1024 if block.block_type == "table" else limit)
        windows = (
            _split_table(block.text, limit)
            if block.block_type == "table"
            else _split_tokens(
                block.text,
                limit,
                settings.chunk_overlap_tokens if block.block_type in {"text", "list", "code"} else 0,
            )
        )
        pieces.extend((block, value) for value in windows if value.strip())
    if not pieces:
        return []
    output: list[Chunk] = []
    for index, (block, text) in enumerate(pieces):
        content = _with_context(block, text)
        locator = {
            key: value
            for key, value in context.raw.get("source_locator", {}).items()
            if key in {
                "page_number", "paragraph_index", "slide_number", "sheet_name",
                "row_start", "row_end", "column_start", "column_end", "char_start", "char_end",
            }
        }
        locator.setdefault("paragraph_index", block.order)
        if block.page_number is not None:
            locator.setdefault("page_number", block.page_number)
        for key in ("slide_number", "sheet_name"):
            if block.metadata.get(key) is not None:
                locator.setdefault(key, block.metadata[key])
        chunk = Chunk.create(
            context=context,
            snapshot_id=snapshot_id,
            chunk_index=index,
            chunk_count=len(pieces),
            content=content,
            variant=variant,
            processing_version=processing_version,
            chunking_version=chunking_version,
            title=" / ".join(block.heading_path) or context.title,
            file_name=context.file_name,
            heading_path=block.heading_path,
            source_locator=locator,
        )
        chunk.rag_eligible = context.resource_type == "message" or context.raw.get("content_type") != "attachment_metadata"
        output.append(chunk)
    return output


def parsed_from_text(text: str, *, parser: str = "inline-text") -> ParsedDocument:
    value = str(text or "").strip()
    return ParsedDocument(
        markdown=value,
        blocks=[CanonicalBlock(None, 0, "text", value)] if value else [],
        parser=parser,
        parser_version="v1",
    )


def _logical_blocks(blocks: Iterable[CanonicalBlock], context: ResourceContext) -> list[_LogicalBlock]:
    output: list[_LogicalBlock] = []
    pending_heading: tuple[str, ...] = ()
    for block in sorted(blocks, key=lambda item: item.order):
        heading = block.heading_path or pending_heading
        if block.type == "title":
            pending_heading = heading or (block.text,)
            continue
        text = block.searchable_text.strip()
        if not text:
            continue
        metadata = dict(block.metadata)
        if (context.file_name or "").lower().endswith((".csv", ".tsv", ".xlsx")):
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
    return re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|\s+|[^\w\s]", value, flags=re.UNICODE)
