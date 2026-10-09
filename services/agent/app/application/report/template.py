"""Parse a .docx weekly-report template into fillable slots.

The template may be a real Word layout or a Markdown-flavoured document (the
current template uses ``## 标题``, ``**字段：** [占位符]`` and ``| 表格 |`` lines
stored as paragraphs). Parsing produces a stable slot list; the content model
fills slot values and the renderer writes them back paragraph by paragraph, so
the template structure is never regenerated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from docx import Document

PLACEHOLDER = re.compile(r"\[([^\]]+)\]")
FIELD = re.compile(r"^\*\*(?P<label>[^*]+)：\*\*\s*(?P<value>.*)$")
HEADING = re.compile(r"^#{1,6}\s*(?P<title>.+)$")
BULLET = re.compile(r"^[-*]\s+")
NUMBERED = re.compile(r"^\d+\.\s+")
TABLE_ROW = re.compile(r"^\|.*\|\s*$")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "_", str(value or "")).strip("_")
    return cleaned or "section"


def _cells(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [cell.strip() for cell in body.split("|")]


@dataclass
class TemplateSlot:
    key: str
    kind: str  # field | table_row | bullet | number | paragraph
    paragraph_index: int
    section: str
    label: str = ""
    columns: list[str] = field(default_factory=list)
    template_text: str = ""

    def prompt(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind,
            "section": self.section,
            "template": self.template_text,
        }
        if self.label:
            value["label"] = self.label
        if self.columns:
            value["columns"] = self.columns
        return value


@dataclass
class TemplateSpec:
    title: str
    paragraphs: list[str]
    slots: list[TemplateSlot]

    def slot_map(self) -> dict[str, TemplateSlot]:
        return {slot.key: slot for slot in self.slots}

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "slots": [slot.prompt() for slot in self.slots],
        }


def parse_template(content: bytes) -> TemplateSpec:
    document = Document(BytesIO(content))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    slots: list[TemplateSlot] = []
    section = ""
    title = ""
    counters: dict[tuple[str, str], int] = {}
    table_buffer: list[tuple[int, str]] = []

    def next_index(kind: str, name: str) -> int:
        key = (kind, name)
        counters[key] = counters.get(key, 0) + 1
        return counters[key] - 1

    def flush_table() -> None:
        nonlocal table_buffer
        if not table_buffer:
            return
        header = _cells(table_buffer[0][1]) if table_buffer else []
        rows = table_buffer[2:]
        section_slug = _slug(section)
        for offset, (index, text) in enumerate(rows):
            slots.append(
                TemplateSlot(
                    key=f"table.{section_slug}.{offset}",
                    kind="table_row",
                    paragraph_index=index,
                    section=section,
                    columns=header,
                    template_text=text,
                )
            )
        table_buffer = []

    for index, text in enumerate(paragraphs):
        stripped = text.strip()
        if TABLE_ROW.match(stripped):
            table_buffer.append((index, stripped))
            continue
        flush_table()
        if not stripped:
            continue
        heading = HEADING.match(stripped)
        if heading:
            if stripped.startswith("### "):
                section = heading.group("title").strip()
            elif not title:
                title = heading.group("title").strip()
                slots.append(
                    TemplateSlot(
                        key="title",
                        kind="title",
                        paragraph_index=index,
                        section="",
                        template_text=stripped,
                    )
                )
            continue
        field_match = FIELD.match(stripped)
        if field_match and (
            PLACEHOLDER.search(field_match.group("value"))
            or field_match.group("label").strip() in {"周期", "日期", "时间"}
        ):
            label = field_match.group("label").strip()
            slots.append(
                TemplateSlot(
                    key=f"field.{_slug(label)}",
                    kind="field",
                    paragraph_index=index,
                    section=section,
                    label=label,
                    template_text=stripped,
                )
            )
            continue
        if BULLET.match(stripped) and PLACEHOLDER.search(stripped):
            offset = next_index("bullet", section)
            slots.append(
                TemplateSlot(
                    key=f"bullet.{_slug(section)}.{offset}",
                    kind="bullet",
                    paragraph_index=index,
                    section=section,
                    template_text=stripped,
                )
            )
            continue
        if NUMBERED.match(stripped) and PLACEHOLDER.search(stripped):
            offset = next_index("number", section)
            slots.append(
                TemplateSlot(
                    key=f"number.{_slug(section)}.{offset}",
                    kind="number",
                    paragraph_index=index,
                    section=section,
                    template_text=stripped,
                )
            )
            continue
        if PLACEHOLDER.search(stripped):
            slots.append(
                TemplateSlot(
                    key=f"paragraph.{index}",
                    kind="paragraph",
                    paragraph_index=index,
                    section=section,
                    template_text=stripped,
                )
            )
    flush_table()
    return TemplateSpec(title=title, paragraphs=paragraphs, slots=slots)
