from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalBlock:
    page_number: int | None
    order: int
    type: str
    text: str = ""
    html: str | None = None
    latex: str | None = None
    asset_ref: str | None = None
    bbox: tuple[float, ...] | None = None
    heading_path: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def searchable_text(self) -> str:
        values = [
            " / ".join(self.heading_path),
            self.text,
            self.html or "",
            self.latex or "",
        ]
        return "\n".join(value for value in values if value).strip()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "page_number": self.page_number,
            "order": self.order,
            "type": self.type,
            "text": self.text,
            "heading_path": list(self.heading_path),
        }
        if self.html is not None:
            result["html"] = self.html
        if self.latex is not None:
            result["latex"] = self.latex
        if self.asset_ref is not None:
            result["asset_ref"] = self.asset_ref
        if self.bbox is not None:
            result["bbox"] = list(self.bbox)
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class ParsedDocument:
    markdown: str
    blocks: list[CanonicalBlock]
    parser: str
    parser_version: str
    artifact_ref: str | None = None
    canonical_ref: str | None = None
    asset_refs: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    auxiliary_files: dict[str, str] = field(default_factory=dict)
