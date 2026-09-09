from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from app.domain.models import CanonicalBlock, ParsedDocument


@dataclass(frozen=True)
class QualityReport:
    status: str
    flags: tuple[str, ...]
    text_length: int
    block_count: int
    asset_count: int


class QualityChecker:
    def check(self, parsed: ParsedDocument) -> QualityReport:
        flags: list[str] = []
        text = parsed.markdown.strip()
        if not text and not parsed.blocks:
            flags.append("empty_content")
        if "\ufffd" in text:
            flags.append("replacement_characters")
        if parsed.blocks and any(block.order < 0 for block in parsed.blocks):
            flags.append("invalid_block_order")
        if len({block.order for block in parsed.blocks}) != len(parsed.blocks):
            flags.append("duplicate_block_order")
        if any(not block.searchable_text.strip() and block.type != "image" for block in parsed.blocks):
            flags.append("empty_text_block")
        for block in parsed.blocks:
            if block.asset_ref:
                path = PurePosixPath(str(block.asset_ref).replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    flags.append("unsafe_asset_reference")
                    break
        if parsed.asset_refs and not all(str(ref).strip() for ref in parsed.asset_refs):
            flags.append("empty_asset_reference")
        status = "failed" if any(flag in flags for flag in ("empty_content", "invalid_block_order", "duplicate_block_order", "unsafe_asset_reference")) else "needs_review" if flags else "passed"
        return QualityReport(status, tuple(flags), len(text), len(parsed.blocks), len(parsed.asset_refs))
