"""Canonical person facts, question modes, and coverage helpers.

The extraction model is allowed to phrase the same fact differently. This
module turns those outputs into stable, comparable fact records so repeated
questions over the same evidence snapshot keep the same fact set, while the
final prose can still vary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.providers.person_facts import PersonFact

OverviewOrFacet = Literal["overview", "facet"]
SNAPSHOT_SCHEMA_VERSION = "person-facts-v2"

CATEGORY_ORDER: tuple[str, ...] = (
    "identity",
    "contact",
    "role",
    "work",
    "project",
    "recent_activity",
    "relationship",
    "preference",
    "other",
)

CATEGORY_LABELS: dict[str, str] = {
    "identity": "身份",
    "contact": "联系方式",
    "role": "角色/单位",
    "work": "工作",
    "project": "项目",
    "recent_activity": "近期活动",
    "relationship": "关系/群组",
    "preference": "偏好",
    "other": "其他",
}

# Final user-facing sections.  The model writes only the text under each
# heading; this ordering and the omission of empty sections stay deterministic.
PERSON_SECTION_ORDER: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("identity", "身份", ("identity",)),
    ("contact", "联系方式", ("contact",)),
    ("role", "角色/单位", ("role",)),
    ("work_project", "工作/项目", ("work", "project")),
    ("recent_activity", "近期活动", ("recent_activity",)),
    ("relationship", "关系/群组", ("relationship",)),
    ("other", "其他", ("preference", "other")),
)

PERSON_SECTION_LABELS: dict[str, str] = {
    key: label for key, label, _categories in PERSON_SECTION_ORDER
}

INTERNAL_CATEGORY_TO_SECTION: dict[str, str] = {
    category: key
    for key, _label, categories in PERSON_SECTION_ORDER
    for category in categories
}

FACT_TYPE_TO_CATEGORY: dict[str, str] = {
    "identity": "identity",
    "nickname": "identity",
    "gender": "identity",
    "student_id": "identity",
    "phone": "contact",
    "email": "contact",
    "address": "contact",
    "wechat_id": "contact",
    "role": "role",
    "school": "role",
    "company": "role",
    "work": "work",
    "task": "work",
    "project": "project",
    "course": "recent_activity",
    "event": "recent_activity",
    "recent_activity": "recent_activity",
    "relationship": "relationship",
    "preference": "preference",
}

OVERVIEW_MARKERS: tuple[str, ...] = (
    "情况",
    "介绍",
    "了解一下",
    "是谁",
    "个人信息",
    "基本信息",
    "资料",
)

FACET_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("project", ("项目", "方案", "进展", "进度", "交付", "技术", "开发")),
    ("work", ("工作", "职责", "负责", "任务", "做什么")),
    ("contact", ("联系方式", "手机号", "电话", "邮箱", "微信", "地址")),
    ("identity", ("身份", "姓名", "名字", "性别", "学号", "是谁")),
    ("role", ("职务", "职位", "角色", "公司", "学校", "单位", "学生")),
    ("recent_activity", ("最近", "近期", "忙什么", "在做什么", "活动")),
    ("relationship", ("关系", "同组", "小组", "群组", "群里")),
    ("preference", ("偏好", "喜欢", "习惯")),
)


@dataclass(frozen=True)
class QuestionMode:
    mode: OverviewOrFacet
    facet: str | None = None


def question_mode(question: str) -> QuestionMode:
    """Classify a person question as overview or one facet.

    Facet markers win over overview markers, because "他的项目情况" is a facet
    question even though it contains the word "情况".
    """

    text = " ".join(str(question or "").split())
    for facet, markers in FACET_MARKERS:
        if any(marker in text for marker in markers):
            return QuestionMode(mode="facet", facet=facet)
    if any(marker in text for marker in OVERVIEW_MARKERS):
        return QuestionMode(mode="overview")
    return QuestionMode(mode="overview")


def normalize_value(value: str) -> str:
    return "".join(
        character
        for character in str(value or "").lower()
        if not character.isspace()
        and character
        not in "，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》-—_·"
    )


def snapshot_fingerprint(chunks: Iterable[dict[str, Any]]) -> str:
    """Stable identity for the evidence set used for one person query."""

    values: list[str] = []
    for item in chunks:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        values.append(f"{item.get('chunk_id') or ''}:{digest}")
    payload = "\n".join([SNAPSHOT_SCHEMA_VERSION, *sorted(values)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonicalize_fact(fact: PersonFact) -> dict[str, Any]:
    category = FACT_TYPE_TO_CATEGORY.get(str(fact.fact_type or ""), "other")
    return {
        "category": category,
        "canonical_key": f"{category}.{str(fact.fact_type or 'other')}",
        "fact_type": str(fact.fact_type or "other"),
        "label": str(fact.label or ""),
        "value": str(fact.value or ""),
        "normalized_value": normalize_value(fact.value),
        "quote": str(fact.quote or ""),
        "speaker_role": (
            "subject" if fact.speaker_role == "subject" else "other"
        ),
        "resource_id": str(fact.resource_id or ""),
        "sent_at": str(fact.sent_at or ""),
    }


def merge_facts(facts: Iterable[PersonFact]) -> list[dict[str, Any]]:
    """Deduplicate and merge facts, preserving conflicting values.

    Same canonical key + same normalized value is one fact. Same canonical key
    with different values remains separate; the newest one is marked current
    and older ones are historical.
    """

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fact in facts:
        item = canonicalize_fact(fact)
        if not item["normalized_value"]:
            continue
        key = (
            item["category"],
            item["canonical_key"],
            item["normalized_value"],
        )
        current = merged.get(key)
        if current is None or item["sent_at"] > current.get("sent_at", ""):
            merged[key] = item

    by_canonical: dict[str, list[dict[str, Any]]] = {}
    for item in merged.values():
        by_canonical.setdefault(item["canonical_key"], []).append(item)

    output: list[dict[str, Any]] = []
    for items in by_canonical.values():
        latest = max(items, key=lambda item: item.get("sent_at", ""))
        latest_identity = (
            latest.get("canonical_key"),
            latest.get("normalized_value"),
            latest.get("sent_at"),
        )
        for item in items:
            copied = dict(item)
            copied["status"] = (
                "current"
                if (
                    item.get("canonical_key"),
                    item.get("normalized_value"),
                    item.get("sent_at"),
                )
                == latest_identity
                else "historical"
            )
            output.append(copied)
    output.sort(
        key=lambda item: (
            CATEGORY_ORDER.index(item["category"])
            if item["category"] in CATEGORY_ORDER
            else len(CATEGORY_ORDER),
            0 if item.get("status") == "current" else 1,
            item["canonical_key"],
            item["value"],
        )
    )
    return output


def profile_sections(
    facts: list[dict[str, Any]],
    *,
    facet: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {}
    for item in facts:
        category = str(item.get("category") or "other")
        if facet and category != facet:
            continue
        sections.setdefault(category, []).append(item)
    return sections


def fact_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for item in items:
        digest = hashlib.sha256(
            (
                f"{item.get('canonical_key')}|{item.get('normalized_value')}|"
                f"{item.get('resource_id')}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        label = str(item.get("label") or item.get("fact_type") or "")
        value = str(item.get("value") or "")
        evidence.append(
            {
                "evidence_id": f"person-fact:{digest}",
                "fetch_method": "person_fact",
                "evidence_kind": "person_fact",
                "attribution": (
                    "subject_said"
                    if item.get("speaker_role") == "subject"
                    else "other_said"
                ),
                "fact_category": item.get("category"),
                "fact_type": item.get("fact_type"),
                "fact_key": item.get("canonical_key"),
                "fact_label": label,
                "fact_value": value,
                "fact_status": item.get("status"),
                "quote": str(item.get("quote") or value),
                "snippet": str(item.get("quote") or value),
                "resource_id": item.get("resource_id"),
                "sent_at": item.get("sent_at") or None,
            }
        )
    return evidence


def missing_fact_values(
    answer: str,
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Facts that are absent from a generated answer.

    The check is semantic at the value level, not textual: the same value may
    be phrased differently. If a value cannot be found, the caller appends a
    short deterministic "补充信息" section so no extracted fact is lost.
    """

    body = normalize_value(answer)
    if not body:
        return list(facts)
    missing: list[dict[str, Any]] = []
    for item in facts:
        value = str(item.get("normalized_value") or "")
        if value and value not in body:
            missing.append(item)
    return missing


def coverage_summary(
    facts: list[dict[str, Any]],
    *,
    mode: QuestionMode,
) -> dict[str, Any]:
    sections = profile_sections(facts, facet=mode.facet)
    return {
        "mode": mode.mode,
        "facet": mode.facet,
        "facts": len(facts),
        "categories": {
            category: len(sections.get(category) or [])
            for category in CATEGORY_ORDER
            if sections.get(category)
        },
    }


def facts_by_person_section(
    facts: Iterable[dict[str, Any]],
    *,
    facet: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Group canonical facts into the final user-facing sections."""

    wanted = INTERNAL_CATEGORY_TO_SECTION.get(str(facet or ""))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in facts:
        category = str(item.get("category") or "other")
        section = INTERNAL_CATEGORY_TO_SECTION.get(category, "other")
        if wanted and section != wanted:
            continue
        grouped.setdefault(section, []).append(item)
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                str(item.get("sent_at") or "9999"),
                str(item.get("canonical_key") or ""),
                str(item.get("value") or ""),
            )
        )
    return grouped


def _fact_date(value: Any, timezone_name: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and timezone_name:
            try:
                parsed = parsed.astimezone(ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError:
                pass
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        return text[:10] if len(text) >= 10 else text


def _fact_bullet(
    item: dict[str, Any],
    *,
    timezone_name: str | None = None,
) -> str:
    value = str(item.get("value") or "").strip()
    if not value:
        return ""
    date = _fact_date(item.get("sent_at"), timezone_name)
    status = str(item.get("status") or "")
    status_label = {
        "current": "当前",
        "historical": "历史",
    }.get(status, "")
    meta = "、".join(value for value in (date, status_label) if value)
    prefix = f"（{meta}）" if meta else ""
    if str(item.get("speaker_role") or "") == "subject":
        content = value
    else:
        # Keep the user-facing wording neutral without exposing an attribution
        # badge such as "other_said".
        content = f"对话中提到：{value}"
    return f"- {prefix}{content}" if prefix else f"- {content}"


def render_person_sections(
    sections: dict[str, list[dict[str, Any]]],
    *,
    summaries: dict[str, str] | None = None,
    timezone_name: str | None = None,
) -> str:
    """Render fixed headings and omit every empty section."""

    summaries = summaries or {}
    parts: list[str] = []
    for key, label, _categories in PERSON_SECTION_ORDER:
        items = sections.get(key) or []
        summary = str(summaries.get(key) or "").strip()
        if not items and not summary:
            continue
        parts.append(f"## {label}")
        if summary:
            parts.append(summary)
            summary_text = normalize_value(summary)
            missing = [
                item
                for item in items
                if str(item.get("normalized_value") or "")
                and str(item.get("normalized_value") or "") not in summary_text
            ]
        else:
            missing = items
        seen_values: set[str] = set()
        for item in missing:
            value = str(item.get("normalized_value") or "")
            if not value or value in seen_values:
                continue
            seen_values.add(value)
            bullet = _fact_bullet(item, timezone_name=timezone_name)
            if bullet:
                parts.append(bullet)
    return "\n\n".join(parts)
