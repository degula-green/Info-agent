"""Is the subject the user named a person, or an organization?

Person questions and form filling ask the same question -- "whose data is
this?" -- and both must route a company somewhere else. The label has to come
from the name itself: ``ResolvePerson`` returning no match cannot mean
"organization", because a person who was never attached or collected looks
exactly the same, and those turns must keep going through the person path.

The marker list mirrors ``contactname.hardMarkers`` in the knowledge service,
which already strips these words when it reduces a contact's display name to a
person's name core ("飞鱼公司张三" -> "张三").
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger("agent.subject")

SubjectIntent = Literal["form.complete", "report.weekly", "person.query"]
SubjectKind = Literal["self", "person", "organization", "unspecified"]


@dataclass(frozen=True)
class SubjectMention:
    """One subject mention extracted for one intent.

    ``mention`` is the text the caller should carry into entity resolution.
    ``kind`` is deliberately coarse: the capability that owns the intent decides
    what ``self`` or ``unspecified`` means for its own product object.
    """

    kind: SubjectKind
    mention: str = ""
    confidence: float = 0.0
    reason: str = ""
    source: str = "rules"


SELF_REFERENCES = frozenset(
    {
        "我",
        "我的",
        "自己",
        "本人",
        "我自己",
        "我本人",
        "我的周报",
    }
)

# These are single tokens that carry no identity. A model or a narrow rule may
# still hand one back when the real subject is absent; it must not become a
# person or a form scope.
_INVALID_SUBJECT_FRAGMENTS = frozenset(
    {
        "上",
        "下",
        "这",
        "那",
        "前",
        "后",
        "本",
        "一",
        "一下",
        "一份",
        "一张",
        "一篇",
        "一个",
        "份",
        "张",
        "篇",
        "个",
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "或",
        "把",
        "将",
        "给",
        "帮",
        "请",
        "填写",
        "填入",
        "填进",
        "填",
        "写",
        "生成",
        "整理",
        "做",
        "弄",
        "周报",
        "报告",
        "表单",
        "表格",
        "申请表",
        "信息",
        "资料",
        "内容",
        "情况",
        "数据",
    }
)

# Longest alternatives first: without that, "上一周" wins inside "上上一周"
# and leaves a trailing "上" that looks like a name.
_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日号]?"
        r"\s*(?:至|到|~|—|-)\s*"
        r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日号]?"
    ),
    re.compile(
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
        r"\s*(?:至|到|~|—|-)\s*"
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    ),
    re.compile(
        r"(?:上上|前前|下下|上|下|这|本|前)"
        r"(?:一)?(?:个)?(?:自然)?(?:周|星期|礼拜)"
    ),
    re.compile(r"(?:近|最近|过去)(?:一|两|三|几)?(?:周|个?星期)"),
    re.compile(r"今天|今日|昨天|昨日|前天|大前天|明天|明日|后天|大后天"),
    re.compile(r"最近|近期"),
)

_FORM_SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:把|将)\s*[\"'“”‘’【】\[]?(?P<subject>[^，。；、\s\"'“”‘’【】\[\]]{1,30}?)"
        r"[\"'“”‘’【】\]]?\s*的(?:信息|资料|内容|情况|数据)"
    ),
    re.compile(
        r"(?:填写|填入|录入|填|写)\s*[\"'“”‘’【】\[]?(?P<subject>[^，。；、\s\"'“”‘’【】\[\]]{1,30}?)"
        r"[\"'“”‘’【】\]]?\s*的?(?:信息|资料|内容|情况|数据)"
    ),
    re.compile(
        r"(?:把|将)\s*[\"'“”‘’【】\[]?(?P<subject>[^，。；、\s\"'“”‘’【】\[\]]{1,30}?)"
        r"[\"'“”‘’【】\]]?\s*(?:信息|资料|内容|情况|数据)\s*"
        r"(?:填写|填入|填进|填到|填|录入|写到|写进|写)"
    ),
)

_FORM_SUBJECT_NOISE = (
    "填写",
    "填入",
    "录入",
    "帮我",
    "帮忙",
    "请",
    "麻烦",
    "根据",
    "知识库",
    "里面的内容",
    "里面的",
    "内容",
    "把",
    "将",
)

_REPORT_NAME_PATTERNS = (
    re.compile(
        r"给(?P<name>[\u4e00-\u9fa5A-Za-z0-9_\-·]{1,20}?)"
        r"(?:写|生成|整理|做|出|来|弄)"
    ),
    re.compile(r"(?:写|生成|整理|做|出|来|弄)(?P<name>[\u4e00-\u9fa5A-Za-z0-9_\-·]{1,20}?)的周报"),
    re.compile(r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9_\-·]{1,20}?)的(?:上一周|上周|本周|这周|个人)?周报"),
)

_REPORT_MEASURE_WORDS = ("一份", "一张", "一篇", "一个")
_REPORT_BARE_MEASURE_WORDS = ("份", "张", "篇", "个")
_REPORT_TIME_WORD = re.compile(
    r"(?:上上|下下|前前|上|下|这|本|前)"
    r"(?:一)?(?:个)?(?:自然)?(?:周|星期|礼拜)"
)

_PERSON_QUERY_MARKERS = (
    "的信息",
    "的情况",
    "的项目情况",
    "的工作情况",
    "的项目进展",
    "的工作进展",
    "联系方式",
    "微信",
    "地址",
    "个人情况",
    "基本情况",
    "是谁",
    "介绍",
    "是多少",
    "多少",
    "手机号",
    "邮箱",
    "职位",
    "职务",
    "公司",
    "负责什么",
    "负责哪些",
    "负责的",
    "最近在",
    "最近忙",
    "在忙什么",
    "怎么样",
    "做了什么",
    "说过什么",
    "提到过",
    "参与",
)

_PERSON_NAME_PREFIX = re.compile(
    r"(?:我想要知道|我想知道|我想了解|想要了解|了解一下|帮我介绍|"
    r"问一下|问问|查一下|查查|帮我查|帮我问|帮我看看|看一下|看看|关于)\s*"
    r"(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_\-]{1,19})"
)
_PERSON_NAME_START = re.compile(
    r"^(?P<name>[\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z0-9_\-]{1,19})"
    r"(?=负责|的|最近|在|说|提到|参与|做|忙|情况|信息|是谁)"
)

ORGANIZATION_MARKERS: tuple[str, ...] = (
    "公司",
    "集团",
    "企业",
    "银行",
    "科技",
    "工作室",
    "部门",
    "中心",
    "学院",
    "大学",
    "学校",
    "团队",
    "小组",
    "事务所",
    "研究院",
    "协会",
)

# "我们的公司" and "我司" mean the same subject as "公司"; the sources index
# only ever holds the bare word.
SELF_REFERENCE_PREFIXES: tuple[str, ...] = (
    "我们的",
    "我们组",
    "我的",
    "我司",
    "本公司",
    "我们",
    "我",
)

_PERSON_NAME = re.compile(r"(?:[\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z0-9_\-]{1,19})")
_ORG_IN_TEXT = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9]{1,12}?(?:" + "|".join(ORGANIZATION_MARKERS) + r")"
)
_LEAD_NOISE = (
    "帮我把",
    "帮我",
    "把",
    "将",
    "问一下",
    "问问",
    "查一下",
    "查查",
    "关于",
    "看看",
    "看一下",
    "请",
    "麻烦",
)


def subject_core(name: str) -> str:
    """Drop a self-reference prefix: "我们的公司" -> "公司"."""

    text = str(name or "").strip()
    for prefix in SELF_REFERENCE_PREFIXES:
        if text.startswith(prefix):
            # "我司" is the whole subject, not a prefix plus an empty tail.
            return text[len(prefix):].strip() or text
    return text


def subject_is_organization(name: str) -> bool:
    """True when a subject name denotes an organization rather than a person.

    A trailing marker ("深空公司", "中国银行") is an organization. A marker
    with a person's name after it ("飞鱼公司张三") is a person whose contact
    label carries the company, which is exactly what the knowledge service's
    name-core rule treats as a person.
    """

    core = subject_core(name)
    if not core:
        return False
    index = -1
    marker = ""
    for candidate in ORGANIZATION_MARKERS:
        position = core.rfind(candidate)
        if position > index or (position == index and len(candidate) > len(marker)):
            index, marker = position, candidate
    if index < 0:
        return False
    tail = core[index + len(marker):].strip()
    if not tail:
        return True
    return not _PERSON_NAME.fullmatch(tail)


def organization_subject_in(text: str) -> str:
    """The organization named in a sentence, or "" when there is none.

    Used when the name extractors could not read a subject at all: the
    classifier may still have named a person intent ("深空公司那边最近怎么
    样"), and the organization word is the only signal that it is not a person.
    """

    found = ""
    for match in _ORG_IN_TEXT.finditer(str(text or "")):
        candidate = _strip_lead_noise(match.group(0))
        if candidate:
            found = candidate
    return found


def _strip_lead_noise(value: str) -> str:
    text = str(value or "").strip()
    for marker in _LEAD_NOISE:
        if text.startswith(marker) and len(text) > len(marker):
            return _strip_lead_noise(text[len(marker):])
    return text


def find_time_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Character spans that name a time expression, longest match first."""

    value = str(text or "")
    spans: list[tuple[int, int]] = []
    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(value):
            start, end = match.span()
            overlaps = any(
                start < existing_end and end > existing_start
                for existing_start, existing_end in spans
            )
            if overlaps:
                continue
            spans.append((start, end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def remove_time_spans(text: str) -> str:
    """Drop time phrases before a subject candidate is selected.

    The time slot is still parsed separately by the capability that needs it;
    this view exists only so "上上一周" cannot become a person or a scope.
    """

    value = str(text or "")
    spans = find_time_spans(value)
    if not spans:
        return " ".join(value.split())
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(value[cursor:start])
        cursor = end
    parts.append(value[cursor:])
    return " ".join("".join(parts).split())


def _clean_candidate(value: str) -> str:
    text = str(value or "").strip().strip(
        " \t\r\n，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》"
    )
    return " ".join(text.split()).strip().strip("的").strip()


def _clean_subject_value(value: str) -> str:
    text = _clean_candidate(value)
    for marker in _FORM_SUBJECT_NOISE:
        index = text.rfind(marker)
        if index >= 0:
            text = text[index + len(marker):]
    return " ".join(text.split()).strip().strip("的").strip()


def _clean_person_name_for_subject(value: str) -> str:
    name = _clean_candidate(value)
    name = re.split(
        r"(?:上个月|上月|本月|这个月|今天|今日|昨天|最近|近|在|从)",
        name,
        maxsplit=1,
    )[0].strip()
    for prefix in ("帮我", "请", "查一下", "找一下", "搜索", "检索", "关于", "用户"):
        if name.startswith(prefix) and len(name) > len(prefix) + 1:
            name = name[len(prefix):]
    if name in {"谁", "谁发", "哪个", "哪个人", "什么人"}:
        return ""
    return name[:20]


def _trim_person_name_for_subject(value: str) -> str:
    name = _clean_person_name_for_subject(value)
    for marker in ("负责", "关于", "的", "情况", "信息", "提到", "参与", "说", "做", "忙"):
        index = name.find(marker)
        if index > 0:
            name = name[:index]
    return name.strip()


def _strip_report_frame(name: str) -> str:
    text = _clean_person_name_for_subject(name)
    for word in _REPORT_MEASURE_WORDS:
        if text.startswith(word):
            text = text[len(word):]
            break
    else:
        for word in _REPORT_BARE_MEASURE_WORDS:
            if text.startswith(word) and _REPORT_TIME_WORD.search(text[len(word):]):
                text = text[len(word):]
                break
    text = _REPORT_TIME_WORD.sub("", text)
    return text.strip().strip("的").strip()


def _is_invalid_candidate(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if text in _INVALID_SUBJECT_FRAGMENTS:
        return True
    if len(text) == 1 and text not in SELF_REFERENCES:
        return True
    if text.isdigit():
        return True
    if re.search(r"https?://", text, re.IGNORECASE):
        return True
    if find_time_spans(text):
        return True
    if re.fullmatch(r"(?:我|你|他|她|它)的?[上前后下]", text):
        return True
    return False


def classify_subject_candidate(
    value: str,
    *,
    intent: SubjectIntent,
    confidence: float = 0.65,
    source: str = "rules",
    reason: str = "",
) -> SubjectMention:
    """Classify an already-cleaned candidate for one intent."""

    del intent  # The caller owns the intent-specific default.
    cleaned = _clean_candidate(value)
    if not cleaned:
        return SubjectMention(
            kind="unspecified",
            confidence=0.0,
            source=source,
            reason=reason or "no explicit subject",
        )
    if cleaned in SELF_REFERENCES:
        return SubjectMention(
            kind="self",
            mention="我",
            confidence=max(confidence, 0.9),
            source=source,
            reason=reason or "self reference",
        )
    if _is_invalid_candidate(cleaned):
        return SubjectMention(
            kind="unspecified",
            confidence=0.0,
            source=source,
            reason=reason or f"invalid subject fragment: {cleaned}",
        )
    if subject_is_organization(cleaned):
        return SubjectMention(
            kind="organization",
            mention=cleaned,
            confidence=confidence,
            source=source,
            reason=reason or "organization marker",
        )
    if len(cleaned) < 2 and cleaned not in SELF_REFERENCES:
        return SubjectMention(
            kind="unspecified",
            confidence=0.0,
            source=source,
            reason=reason or "candidate too short",
        )
    return SubjectMention(
        kind="person",
        mention=cleaned,
        confidence=confidence,
        source=source,
        reason=reason or "person candidate",
    )


def _mention_in_text(mention: str, text: str) -> bool:
    def normalise(value: str) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    return bool(mention) and normalise(mention) in normalise(text)


def validate_subject_mention(
    mention: SubjectMention | None,
    *,
    text: str,
    intent: SubjectIntent,
) -> SubjectMention | None:
    """Normalise and validate a model answer before any caller consumes it."""

    del intent  # Intent-specific defaults belong to the thin callers.
    if mention is None:
        return None
    kind = str(getattr(mention, "kind", "")).strip()
    value = _clean_candidate(getattr(mention, "mention", ""))
    confidence = max(0.0, min(1.0, float(getattr(mention, "confidence", 0.0) or 0.0)))
    reason = str(getattr(mention, "reason", "") or "")
    source = str(getattr(mention, "source", "model") or "model")
    if value in SELF_REFERENCES:
        return SubjectMention(
            kind="self",
            mention="我",
            confidence=max(confidence, 0.8),
            source=source,
            reason=reason or "self reference",
        )
    if kind == "unspecified":
        return SubjectMention(
            kind="unspecified",
            confidence=confidence,
            source=source,
            reason=reason,
        )
    if kind == "self":
        return SubjectMention(
            kind="self",
            mention="我",
            confidence=max(confidence, 0.8),
            source=source,
            reason=reason,
        )
    if kind not in {"person", "organization"} or not value:
        return None
    if not _mention_in_text(value, text):
        return None
    if _is_invalid_candidate(value):
        return None
    is_organization = subject_is_organization(value)
    if kind == "organization" and not is_organization:
        kind = "person"
    elif kind == "person" and is_organization:
        kind = "organization"
    return SubjectMention(
        kind=kind,  # type: ignore[arg-type]
        mention=value,
        confidence=confidence,
        source=source,
        reason=reason,
    )


def _try_model_subject(
    text: str,
    *,
    intent: SubjectIntent,
    extractor: Any | None,
) -> SubjectMention | None:
    if extractor is None:
        return None
    try:
        raw = extractor.extract(text, intent=intent)
    except Exception as exc:  # noqa: BLE001 - optional path must not fail a turn
        logger.warning("subject extractor failed for %s: %s", intent, exc)
        return None
    return validate_subject_mention(raw, text=text, intent=intent)


def _rule_form_subject(text: str) -> SubjectMention:
    for pattern in _FORM_SUBJECT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        subject = _clean_subject_value(match.group("subject"))
        if subject:
            return classify_subject_candidate(
                subject,
                intent="form.complete",
                reason="form frame",
            )
    return SubjectMention(kind="unspecified", reason="no form subject frame")


def _rule_report_subject(text: str) -> SubjectMention:
    name = ""
    match = _REPORT_NAME_PATTERNS[0].search(text)
    if match:
        name = _clean_person_name_for_subject(match.group("name"))
    if not name:
        for pattern in _REPORT_NAME_PATTERNS[1:]:
            match = pattern.search(text)
            if match:
                name = _clean_person_name_for_subject(match.group("name"))
                break
    name = _strip_report_frame(name)
    return classify_subject_candidate(
        name,
        intent="report.weekly",
        reason="report frame",
    )


def _rule_person_subject(text: str) -> SubjectMention:
    if not any(marker in text for marker in _PERSON_QUERY_MARKERS):
        return SubjectMention(kind="unspecified", reason="no person question marker")
    name = ""
    match = _PERSON_NAME_START.match(text)
    if match:
        name = _trim_person_name_for_subject(match.group("name"))
    if not name:
        match = _PERSON_NAME_PREFIX.search(text)
        if match:
            name = _trim_person_name_for_subject(match.group("name"))
    return classify_subject_candidate(
        name,
        intent="person.query",
        reason="person question frame",
    )


def extract_subject_mention(
    text: str,
    *,
    intent: SubjectIntent,
    extractor: Any | None = None,
) -> SubjectMention:
    """Extract one subject for ``intent``.

    A configured model extractor is the primary path. If it fails, returns an
    invalid answer, or is absent, a narrow deterministic fallback keeps the
    existing behaviour. The model may legitimately answer ``unspecified``; that
    is a verdict, not a failure, and the caller applies its own default.
    """

    original = str(text or "")
    if not original.strip():
        return SubjectMention(kind="unspecified", reason="empty input")
    model_mention = _try_model_subject(
        original,
        intent=intent,
        extractor=extractor,
    )
    if model_mention is not None and model_mention.confidence >= 0.5:
        return model_mention
    without_time = remove_time_spans(original)
    if intent == "form.complete":
        return _rule_form_subject(without_time)
    if intent == "report.weekly":
        # Report patterns already separate the person from the verb. They
        # consume the original wording so a surname such as 张 is not mistaken
        # for the measure word "张"; _strip_report_frame removes week wording.
        return _rule_report_subject(original)
    if intent == "person.query":
        return _rule_person_subject(without_time)
    return SubjectMention(kind="unspecified", reason=f"unsupported intent: {intent}")


__all__ = [
    "ORGANIZATION_MARKERS",
    "SELF_REFERENCE_PREFIXES",
    "SELF_REFERENCES",
    "SubjectIntent",
    "SubjectKind",
    "SubjectMention",
    "classify_subject_candidate",
    "extract_subject_mention",
    "find_time_spans",
    "organization_subject_in",
    "remove_time_spans",
    "subject_core",
    "subject_is_organization",
    "validate_subject_mention",
]
