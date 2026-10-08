"""Render a weekly report by filling the template's parsed slots.

The template is used as the base document: only the slot paragraphs are
rewritten, so headings, guidance text and table headers keep the template's
exact structure. Complex Word styling is out of scope for V1.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from docx import Document

from app.application.report.template import TemplateSlot, parse_template

BOLD_LABEL = re.compile(r"^[-*]\s+\*\*(?P<label>[^*]+)：\*\*\s*")


def render_report(template_bytes: bytes, values: dict[str, Any]) -> bytes:
    document = Document(BytesIO(template_bytes))
    spec = parse_template(template_bytes)
    for slot in spec.slots:
        if slot.key not in values:
            continue
        paragraph = document.paragraphs[slot.paragraph_index]
        paragraph.text = _slot_text(slot, values[slot.key])
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _slot_text(slot: TemplateSlot, value: Any) -> str:
    text = _as_text(value)
    if slot.kind == "title":
        return f"## {text}" if slot.template_text.startswith("##") else text
    if slot.kind == "field":
        return f"**{slot.label}：** {text}"
    if slot.kind == "table_row":
        cells = value if isinstance(value, (list, tuple)) else [text]
        return "| " + " | ".join(str(cell) for cell in cells) + " |"
    if slot.kind == "bullet":
        match = BOLD_LABEL.match(slot.template_text)
        if match:
            return f"- **{match.group('label').strip()}：** {text}"
        return f"- {text}"
    if slot.kind == "number":
        prefix = slot.template_text.split(".", 1)[0]
        return f"{prefix}. {text}"
    return text


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value)
