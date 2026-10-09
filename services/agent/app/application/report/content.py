"""Turn a model's slot values into the exact values the renderer writes.

The model is a source of content, not of policy. This module applies the rules
the plan fixes: no person pronouns, no private-life material, "无" for an empty
section, "根据已有信息暂时未推断出来" for a field the archive cannot answer, and
table rows shaped exactly like the template's columns.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.application.report.period import WeekWindow
from app.application.report.template import TemplateSlot, TemplateSpec

EMPTY_SECTION = "无"
NOT_INFERRED = "根据已有信息暂时未推断出来"
PERSON_FIELD_LABELS = {"汇报人", "姓名", "联系人"}
PERIOD_FIELD_LABELS = {"周期", "日期", "时间", "周次"}

# The user asked for work content only: "我不希望看到他的孩子生病请假这类没有用的消息".
_PRIVATE_MARKERS = (
    "孩子",
    "宝宝",
    "儿子",
    "女儿",
    "老婆",
    "老公",
    "女朋友",
    "男朋友",
    "妈妈",
    "爸爸",
    "生病",
    "发烧",
    "住院",
    "请假",
    "医院",
    "婚礼",
    "生日",
    "纪念日",
    "旅游",
    "度假",
    "电影",
    "追剧",
    "游戏",
    "减肥",
    "房租",
    "彩票",
)

_PRONOUN_PREFIX = re.compile(r"^(?:我|他|她|本人|其)(?:们)?")
_FIXED_LABEL = re.compile(r"^(?:\d+|[Pp]\d)$")
# Markdown the model tends to re-emit for list slots the template already
# formats. The template owns the marker and the bold label, so the value is
# only the sentence.
_LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")
_BOLD_LABEL = re.compile(r"^\*\*[^*]{1,40}[：:]\*\*\s*")
_TEMPLATE_BOLD_LABEL = re.compile(r"\*\*[^*]{1,40}[：:]\*\*")
_EMPTY_VALUES = {"", "无", "暂无", "-", "—", "n/a", "na", "none", "null"}
# "无具体动作与结果" says nothing, but the plan asks an empty section to say 无.
# A sentence built on 无 followed by a placeholder noun is folded to 无; wording
# that actually negates an action ("无法完成") is left alone.
_NO_INFO = re.compile(
    r"^(?:无|暂无|没有)(?:具体|相关|明确|可见|可复用|长期|发现|推进|可提供|更多|任何|相应|特殊|"
    r"信息|内容|数据|记录|情况|事项|动作|结果|问题|建议|支持|阶段|结论|计划|目标|方法|模板|说明|背景|选项|期望)"
)


def strip_pronouns(value: str) -> str:
    """Drop a leading person pronoun so every line reads as an action."""

    text = str(value or "").strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _PRONOUN_PREFIX.sub("", text).strip()
    return text


def is_private_life(value: str) -> bool:
    text = str(value or "")
    return any(marker in text for marker in _PRIVATE_MARKERS)


def clean_text(value: Any) -> str:
    """One work line: no pronouns, no private-life content, no stray whitespace."""

    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value if str(item).strip())
    text = " ".join(str(value).split())
    if not text or is_private_life(text):
        return ""
    return strip_pronouns(text)


def is_empty_value(value: str) -> bool:
    """Whether a cell says "nothing" instead of naming something."""

    return str(value or "").strip().lower() in _EMPTY_VALUES


def normalize_empty_text(value: str) -> str:
    """Fold a "no information" sentence back to the plan's 无."""

    text = str(value or "").strip()
    if not text:
        return text
    if _NO_INFO.match(text):
        return EMPTY_SECTION
    return text


def clean_list_text(value: Any, template_text: str) -> str:
    """A bullet/number value without the marker or label the template supplies."""

    text = clean_text(value)
    if not text:
        return ""
    stripped = _LIST_PREFIX.sub("", text).strip()
    if _TEMPLATE_BOLD_LABEL.search(str(template_text or "")):
        without_label = _BOLD_LABEL.sub("", stripped).strip()
        if without_label:
            stripped = without_label
    return normalize_empty_text(stripped)


def _template_first_cell(slot: TemplateSlot) -> str:
    body = slot.template_text.strip().strip("|")
    cells = [cell.strip() for cell in body.split("|")]
    if not cells:
        return ""
    first = cells[0]
    return first if _FIXED_LABEL.match(first) else ""


def _table_row(slot: TemplateSlot, value: Any) -> tuple[bool, list[str]]:
    """Normalise one row to the template's column count."""

    columns = len(slot.columns) or 1
    label = _template_first_cell(slot)
    if isinstance(value, (list, tuple)):
        raw = list(value)
    elif value in (None, ""):
        raw = []
    else:
        raw = [value]
    cells = [normalize_empty_text(clean_text(item)) for item in raw]
    # A fixed first column (序号 / P0) belongs to the template. When the model
    # returns only the description, the label is put back so the row keeps the
    # template's column meaning instead of shifting every value left by one.
    if label and (not cells or cells[0] != label):
        cells.insert(0, label)
    cells = cells[:columns]
    while len(cells) < columns:
        cells.append("")
    # The row describes something only when its description column survived
    # cleaning. A row whose description was dropped as private life must not
    # keep a stray "已完成" next to an empty subject.
    description_index = 1 if label and len(cells) > 1 else 0
    # "无" in the description column means the model had nothing to say, which
    # is the same as an empty row: the section's once-only 无 covers it.
    return not is_empty_value(cells[description_index]), cells


def _empty_row(slot: TemplateSlot, *, announce_empty: bool) -> list[str]:
    columns = len(slot.columns) or 1
    cells = [""] * columns
    label = _template_first_cell(slot)
    if label:
        cells[0] = label
    if announce_empty:
        for index in range(1 if label else 0, columns):
            cells[index] = EMPTY_SECTION
    return cells


def _field_value(
    slot: TemplateSlot,
    raw: Any,
    person_name: str,
    window: WeekWindow,
) -> str:
    label = str(slot.label or "").strip()
    if label in PERSON_FIELD_LABELS:
        return str(person_name or "").strip() or NOT_INFERRED
    if label in PERIOD_FIELD_LABELS:
        return window.period_text
    # A field describes the person, not a section: when the archive cannot
    # answer it, say so instead of writing 无 (which means "empty section").
    value = clean_text(raw)
    if is_empty_value(value):
        return NOT_INFERRED
    if _NO_INFO.match(value):
        return NOT_INFERRED
    return value


def build_slot_values(
    *,
    spec: TemplateSpec,
    draft_values: dict[str, Any],
    person_name: str,
    window: WeekWindow,
) -> dict[str, Any]:
    """The values dict handed to ``render_report``."""

    values: dict[str, Any] = {}
    sections_with_content: dict[str, bool] = {}
    prepared: list[tuple[TemplateSlot, bool, list[str]]] = []

    for slot in spec.slots:
        raw = (draft_values or {}).get(slot.key)
        if slot.kind == "title":
            # The template's own title is preserved; the week is announced in
            # the 周期 field so the heading still matches the template.
            continue
        if slot.kind == "table_row":
            has_content, cells = _table_row(slot, raw)
            sections_with_content[slot.section] = (
                sections_with_content.get(slot.section, False) or has_content
            )
            prepared.append((slot, has_content, cells))
            continue
        if slot.kind == "field":
            values[slot.key] = _field_value(slot, raw, person_name, window)
            continue
        if slot.kind in ("bullet", "number"):
            values[slot.key] = clean_list_text(raw, slot.template_text) or EMPTY_SECTION
            continue
        values[slot.key] = clean_text(raw) or EMPTY_SECTION

    announced: set[str] = set()
    for slot, has_content, cells in prepared:
        if has_content:
            values[slot.key] = cells
            continue
        section = slot.section
        # An empty table section says 无 once, on its first row; the surplus
        # rows keep their template label column so the table keeps its shape.
        announce = (
            not sections_with_content.get(section, False)
            and section not in announced
        )
        announced.add(section)
        values[slot.key] = _empty_row(slot, announce_empty=announce)
    return values


def template_prompt_payload(spec: TemplateSpec, keys: Iterable[str] | None = None) -> dict[str, Any]:
    """The slot list handed to the model, optionally narrowed to chosen keys."""

    payload = spec.prompt_payload()
    if keys is None:
        return payload
    wanted = {str(key) for key in keys}
    payload["slots"] = [slot for slot in payload["slots"] if slot["key"] in wanted]
    return payload
