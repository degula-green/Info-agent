from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.models import SearchRequest


@dataclass(frozen=True)
class QueryPlan:
    normalized_query: str
    use_vector: bool
    prefer_keyword: bool
    terms: tuple[str, ...]
    retrieval_mode: str = "chunk"
    preferred_tree_type: str = "entity"
    time_after: str | None = None
    time_before: str | None = None
    time_resolved: bool = False
    time_unresolved: bool = False
    historical_query: bool = False
    routing_reason: str = ""


_EXACT_HINTS = re.compile(r"(?:[A-Z]{2,}-?\d+|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\b\d+(?:\.\d+)?\b)")
_TIME_HINTS = re.compile(r"(?:\d{4}\s*[年/-]|\d{1,2}\s*月|\d{1,2}\s*[日号]|本周|本月|最近|历史|过去|去年|上次|哪天|什么时候|时间)")
_CONVERSATION_HINTS = ("哪次聊天", "当时", "群里", "谁说过", "消息", "讨论", "聊天", "会话", "发言", "对话", "上次")
_LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _utc(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def _local_bounds(start: datetime, end: datetime) -> tuple[str, str]:
    return _utc(start), _utc(end)


def _parse_bound(value: str | None, *, end: bool = False) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}", raw):
            start = datetime(int(raw), 1, 1, tzinfo=_LOCAL_ZONE)
            finish = datetime(int(raw) + 1, 1, 1, tzinfo=_LOCAL_ZONE)
            return _utc(finish if end else start)
        if re.fullmatch(r"\d{4}[-/]\d{1,2}", raw):
            year, month = (int(part) for part in re.split(r"[-/]", raw))
            start = datetime(year, month, 1, tzinfo=_LOCAL_ZONE)
            finish = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=_LOCAL_ZONE)
            return _utc(finish if end else start)
        if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", raw):
            year, month, day = (int(part) for part in re.split(r"[-/]", raw))
            start = datetime(year, month, day, tzinfo=_LOCAL_ZONE)
            finish = start + timedelta(days=1)
            return _utc(finish if end else start)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_LOCAL_ZONE)
        return _utc(parsed)
    except (TypeError, ValueError, OverflowError):
        return None


def resolve_time_range(query: str, occurred_after: str | None = None, occurred_before: str | None = None) -> tuple[str | None, str | None, bool, bool]:
    """Resolve supported local-language time expressions to UTC ISO bounds.

    Explicit API bounds always win.  The boolean pair is (resolved, unresolved)
    and lets callers distinguish an omitted time from a requested expression
    that this deterministic parser does not understand.
    """
    if occurred_after or occurred_before:
        after = _parse_bound(occurred_after)
        before = _parse_bound(occurred_before, end=True)
        unresolved = bool((occurred_after and after is None) or (occurred_before and before is None))
        return after, before, not unresolved, unresolved

    text = " ".join((query or "").split())
    now = datetime.now(_LOCAL_ZONE)
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})?\s*月?\s*(\d{1,2})?\s*[日号]?", text)
    if match:
        year = int(match.group(1))
        month = int(match.group(2) or 1)
        day = match.group(3)
        start = datetime(year, month, int(day), tzinfo=_LOCAL_ZONE) if day else datetime(year, month, 1, tzinfo=_LOCAL_ZONE)
        if day:
            finish = start + timedelta(days=1)
        elif match.group(2):
            finish = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=_LOCAL_ZONE)
        else:
            finish = datetime(year + 1, 1, 1, tzinfo=_LOCAL_ZONE)
        after, before = _local_bounds(start, finish)
        return after, before, True, False

    iso_match = re.search(r"(\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?)", text)
    if iso_match:
        after = _parse_bound(iso_match.group(1))
        before = _parse_bound(iso_match.group(1), end=True)
        return after, before, bool(after and before), not bool(after and before)

    if "本周" in text:
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return (*_local_bounds(start, now), True, False)
    if "本月" in text:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (*_local_bounds(start, now), True, False)
    if "最近" in text:
        return (*_local_bounds(now - timedelta(days=30), now), True, False)
    return None, None, False, bool(_TIME_HINTS.search(text))


def plan_query(request: SearchRequest) -> QueryPlan:
    normalized = " ".join(request.query.split())
    if not normalized:
        return QueryPlan("", False, True, (), "chunk", "entity", None, None, False, False, False, "empty_query")
    exact = bool(_EXACT_HINTS.search(normalized)) or len(normalized) <= 3
    semantic_words = any(word in normalized for word in ("为什么", "原因", "如何", "方案", "相关", "区别", "解释", "how", "why"))
    use_vector = request.entry in {"global", "ai"} and not (exact and not semantic_words)
    tree_hint = any(word in normalized for word in ("状态", "进展", "任务", "负责人", "阶段", "关系", "截止", "谁负责", "目前"))
    # Open-ended questions need the tree to route first, then scoped document
    # retrieval to supply the detailed answer. Keep fact-oriented questions
    # tree-first so short operational answers do not pull whole documents.
    broad_hint = any(word in normalized for word in ("总结", "综合", "所有", "整体", "分别", "对比", "哪些", "介绍", "了解", "多少", "具体", "内容", "机制", "资料", "信息"))
    time_after, time_before, time_resolved, time_unresolved = resolve_time_range(normalized, request.occurred_after, request.occurred_before)
    conversation = any(word in normalized for word in _CONVERSATION_HINTS)
    explicit_time = bool(request.occurred_after or request.occurred_before or time_resolved or time_unresolved)
    preferred_tree = "session" if conversation or explicit_time else "entity"
    if conversation:
        reason = "conversation_semantics"
    elif explicit_time:
        reason = "time_scope"
    elif tree_hint or broad_hint:
        reason = "entity_semantics"
    else:
        reason = "default_entity"
    # Tree-first is an AI path decision.  Non-AI/global search retains the
    # established chunk/fusion planning behavior.
    if request.entry == "ai" and (request.knowledge_base_id or request.knowledge_base_ids):
        mode = "tree"
    else:
        mode = "fusion" if broad_hint else "tree" if tree_hint else "chunk"
    historical = any(word in normalized for word in ("历史", "过去", "时间线", "历次", "以前", "曾经"))
    return QueryPlan(normalized, use_vector, exact, tuple(normalized.split()), mode, preferred_tree, time_after, time_before, time_resolved, time_unresolved, historical, reason)
