"""Deterministic routing for company knowledge questions.

The LLM planner is useful for ambiguous, multi-step tasks, but knowledge
retrieval is too important to depend on a model choosing the right tools every
time. This module recognizes the stable knowledge intents and emits a fixed
Capability plan before the configured planner gets a chance to drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.capabilities.knowledge import (
    KNOWLEDGE_ANSWER_NAME,
    SEARCH_CONTENT_NAME,
    SEARCH_SOURCES_NAME,
)
from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
    TaskUnderstanding,
)

KnowledgeMode = Literal["sources", "content", "content_with_sources"]

_CONTENT_MARKERS = (
    "说了什么",
    "说了啥",
    "写的什么",
    "写了什么",
    "内容是",
    "什么内容",
    "怎么规定",
    "如何规定",
    "条款",
    "金额",
    "多少钱",
    "日期",
    "什么时候",
    "是什么",
    "怎么办",
    "为什么",
    "如何",
    "怎么",
    "是否",
    "有没有提到",
    "提到了什么",
    "提到",
    "分析",
    "总结",
    "解释",
)
_SOURCE_MARKERS = (
    "谁发",
    "谁发送",
    "谁传",
    "发过",
    "发送过",
    "发的",
    "发的文件",
    "发的消息",
    "发来的",
    "在哪个群",
    "哪个群",
    "哪个聊天",
    "哪条消息",
    "什么时间",
    "什么时候",
    "哪些文件",
    "哪些消息",
    "哪些资料",
    "有没有发",
    "是否发过",
)
_SEARCH_MARKERS = (
    "找一下",
    "查一下",
    "搜索",
    "检索",
    "帮我找",
    "帮我查",
    "找找",
)
_FILE_MARKERS = (
    "文件",
    "附件",
    "文档",
    "表格",
    "图片",
    "pdf",
    "PDF",
    "excel",
    "xlsx",
    "word",
    "docx",
)
_MESSAGE_MARKERS = ("消息", "聊天记录", "群聊", "群里", "私聊")
_ACTION_MARKERS = ("提醒我", "创建待办", "新建待办", "日程", "预约", "安排会议", "加个提醒")
_CHITCHAT = {"你好", "您好", "hello", "hi", "谢谢", "多谢", "再见", "在吗"}

_SENDER_PATTERNS = (
    re.compile(
        r"(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_\-·]{1,19})"
        r"(?:发过|发送过|发的|发来的|发了)"
    ),
    re.compile(r"(?:发送人|发送者|用户|同事)[:：]?\s*(?P<name>[^\s，。,.!?！？]{1,20})"),
)
_CONVERSATION_PATTERN = re.compile(
    r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9_\-]{2,30}(?:群聊|群|频道))"
)


@dataclass(frozen=True)
class KnowledgeRoute:
    mode: KnowledgeMode
    query: str
    source_query: str
    source_arguments: dict[str, Any]
    content_arguments: dict[str, Any]
    objective: str


def classify_knowledge_question(
    text: str,
    *,
    timezone_name: str = "Asia/Shanghai",
    clock: Callable[[], datetime] | None = None,
) -> KnowledgeRoute | None:
    question = " ".join(str(text or "").split()).strip()
    normalized = question.lower().strip(" \t\r\n，。,.!！?？、:：;；")
    if not normalized or normalized in _CHITCHAT:
        return None

    has_content = _contains_any(normalized, _CONTENT_MARKERS)
    has_source = _contains_any(normalized, _SOURCE_MARKERS)
    has_search = _contains_any(normalized, _SEARCH_MARKERS)
    has_file = _contains_any(normalized, _FILE_MARKERS)
    has_message = _contains_any(normalized, _MESSAGE_MARKERS)
    has_action = _contains_any(normalized, _ACTION_MARKERS)

    if has_action and not (has_source or has_content):
        return None
    if not (has_source or has_content or (has_search and (has_file or has_message))):
        return None

    sender_names = _sender_names(question)
    conversation_names = _conversation_names(question)
    occurred_after, occurred_before = _time_range(
        normalized,
        timezone_name=timezone_name,
        clock=clock,
    )
    resource_types = _resource_types(has_file=has_file, has_message=has_message)
    attachment_types = _attachment_types(normalized)

    source_arguments: dict[str, Any] = {
        "sender_names": sender_names,
        "conversation_names": conversation_names,
        "occurred_after": occurred_after,
        "occurred_before": occurred_before,
        "attachment_types": attachment_types,
        "resource_types": resource_types,
    }
    content_arguments: dict[str, Any] = {"query": question}

    if has_source and has_content:
        mode: KnowledgeMode = "content_with_sources"
    elif has_source or (has_search and (has_file or has_message) and not has_content):
        mode = "sources"
    else:
        mode = "content"

    # Pure metadata questions are better served by filters than by BM25 over
    # the whole sentence. Keep a keyword only when it names a concrete object.
    source_query = ""
    if mode == "sources":
        source_query = _source_keyword(question) if not sender_names and not conversation_names else ""
    source_arguments["query"] = source_query

    objective = {
        "sources": "定位知识来源",
        "content": "检索并回答知识内容",
        "content_with_sources": "定位来源并检索内容回答",
    }[mode]
    return KnowledgeRoute(
        mode=mode,
        query=question,
        source_query=source_query,
        source_arguments={key: value for key, value in source_arguments.items() if value not in (None, "", [], False)},
        content_arguments=content_arguments,
        objective=objective,
    )


def build_knowledge_plan(
    route: KnowledgeRoute,
    task: TaskEnvelope,
    capabilities: list[CapabilityDescriptor],
) -> Plan | None:
    registered = {descriptor.name for descriptor in capabilities}
    if SEARCH_CONTENT_NAME not in registered and SEARCH_SOURCES_NAME not in registered:
        return None

    plan_id = str(uuid4())
    steps: list[PlanStep] = []

    def add_step(capability: str, arguments: dict[str, Any]) -> PlanStep:
        step = PlanStep(
            step_id=f"{plan_id}-step-{len(steps) + 1}",
            plan_id=plan_id,
            order=len(steps) + 1,
            capability=capability,
            arguments=arguments,
        )
        steps.append(step)
        return step

    search_content_step: PlanStep | None = None
    if route.mode == "sources":
        if SEARCH_SOURCES_NAME not in registered:
            return None
        add_step(SEARCH_SOURCES_NAME, dict(route.source_arguments))
    elif route.mode == "content":
        if SEARCH_CONTENT_NAME not in registered:
            return None
        search_content_step = add_step(
            SEARCH_CONTENT_NAME,
            {
                **route.content_arguments,
                "restrict_to_resource_ids": False,
            },
        )
    else:
        if (
            SEARCH_SOURCES_NAME not in registered
            or SEARCH_CONTENT_NAME not in registered
        ):
            return None
        add_step(SEARCH_SOURCES_NAME, dict(route.source_arguments))
        search_content_step = add_step(
            SEARCH_CONTENT_NAME,
            {
                **route.content_arguments,
                "resource_ids_ref": {"step": 1, "output": "resource_ids"},
                "restrict_to_resource_ids": True,
            },
        )

    if search_content_step is not None and KNOWLEDGE_ANSWER_NAME in registered:
        add_step(
            KNOWLEDGE_ANSWER_NAME,
            {
                "query": route.content_arguments["query"],
                "results_ref": {
                    "step": search_content_step.order,
                    "output": "results",
                },
                "metadata_coverage_ref": {
                    "step": search_content_step.order,
                    "output": "metadata_coverage",
                },
            },
        )

    if not steps:
        return None
    return Plan(
        plan_id=plan_id,
        task_id=task.task_id,
        objective=route.objective,
        steps=steps,
    )


class KnowledgeRoutingPlanner:
    """Intercepts stable knowledge intents and delegates everything else."""

    def __init__(
        self,
        base,
        *,
        default_timezone: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base = base
        self.default_timezone = default_timezone
        self.clock = clock
        self._last_call_count = 0

    @property
    def name(self) -> str:
        return str(getattr(self.base, "name", "deterministic"))

    @property
    def last_call_count(self) -> int:
        return max(int(self._last_call_count or 0), 0)

    def set_validators(self, validators) -> None:
        attach = getattr(self.base, "set_validators", None)
        if callable(attach):
            attach(validators)

    def set_capabilities(self, capabilities: list[CapabilityDescriptor]) -> None:
        attach = getattr(self.base, "set_capabilities", None)
        if callable(attach):
            attach(capabilities)

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        route = classify_knowledge_question(
            str(task.input.get("text") or ""),
            timezone_name=self.default_timezone,
            clock=self.clock,
        )
        if route is not None:
            plan = build_knowledge_plan(route, task, capabilities)
            if plan is not None:
                self._last_call_count = 0
                return plan
        built = self.base.create_plan(
            task,
            capabilities,
            observations,
            constraints,
            understanding,
        )
        self._last_call_count = max(
            int(getattr(self.base, "last_call_count", 0) or 0),
            0,
        )
        return built

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        if _is_knowledge_plan(current_plan):
            self._last_call_count = 0
            return _decide_knowledge_plan(current_plan, observations)
        decision = self.base.decide_after_observation(
            task,
            current_plan,
            observations,
            constraints,
            understanding,
        )
        self._last_call_count = max(
            int(getattr(self.base, "last_call_count", 0) or 0),
            0,
        )
        return decision


def _decide_knowledge_plan(
    current_plan: Plan,
    observations: list[Observation],
) -> PlannerDecision:
    latest = observations[-1] if observations else None
    if latest is not None and latest.status != "succeeded":
        classification = str((latest.error or {}).get("classification") or "")
        return PlannerDecision(
            action="fail",
            reason=f"knowledge retrieval failed: {classification or 'failed capability'}",
        )
    if any(step.status in {"pending", "ready", "running"} for step in current_plan.steps):
        return PlannerDecision(action="continue")
    return PlannerDecision(action="complete", reason="knowledge result is ready")


def _is_knowledge_plan(plan: Plan) -> bool:
    if not plan.steps:
        return False
    knowledge = {SEARCH_SOURCES_NAME, SEARCH_CONTENT_NAME, KNOWLEDGE_ANSWER_NAME}
    return all(step.capability in knowledge for step in plan.steps)


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value.lower() in text for value in values)


def _sender_names(text: str) -> list[str]:
    names: list[str] = []
    for pattern in _SENDER_PATTERNS:
        for match in pattern.finditer(text):
            name = _clean_person_name(match.group("name"))
            if name and name not in names:
                names.append(name)
    return names[:10]


def _clean_person_name(value: str) -> str:
    name = str(value or "").strip(" \t\r\n，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》")
    name = re.split(
        r"(?:上个月|上月|本月|这个月|今天|今日|昨天|最近|近|在|从)",
        name,
        maxsplit=1,
    )[0].strip()
    for prefix in ("帮我", "请", "查一下", "找一下", "搜索", "检索", "关于", "用户"):
        if name.startswith(prefix) and len(name) > len(prefix) + 1:
            name = name[len(prefix) :]
    if name in {"谁", "谁发", "哪个", "哪个人", "什么人"}:
        return ""
    return name[:20]


def _conversation_names(text: str) -> list[str]:
    names: list[str] = []
    for match in _CONVERSATION_PATTERN.finditer(text):
        name = str(match.group("name") or "").strip().rsplit("在", 1)[-1]
        if name and name not in names:
            names.append(name)
    return names[:10]


def _resource_types(*, has_file: bool, has_message: bool) -> list[str]:
    values: list[str] = []
    if has_file:
        values.append("attachment")
    if has_message:
        values.append("message")
    return values


def _attachment_types(text: str) -> list[str]:
    types: list[str] = []
    if "pdf" in text:
        types.append("pdf")
    if any(value in text for value in ("excel", "xlsx", "xls", "表格")):
        types.append("excel")
    if any(value in text for value in ("word", "docx", "文档")):
        types.append("word")
    if any(value in text for value in ("ppt", "pptx", "演示文稿")):
        types.append("ppt")
    if any(value in text for value in ("图片", "照片", "截图")):
        types.append("image")
    return list(dict.fromkeys(types))


def _source_keyword(text: str) -> str:
    value = str(text or "").strip()
    for prefix in ("帮我找一下", "帮我查一下", "找一下", "查一下", "搜索", "检索", "帮我找", "帮我查"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    value = re.sub(r"(是谁发的|谁发的|发过哪些|发过什么|哪些文件|哪些消息|哪些资料)", " ", value)
    value = re.sub(r"(的文件|的消息|的资料|文件|消息|资料)$", " ", value)
    cleaned = " ".join(value.strip(" \t\r\n，。,.!！?？、:：;；").split())[:120]
    if cleaned in {"谁", "哪些", "什么", "哪个", "哪条", "哪个群", "什么时候", "什么时间"}:
        return ""
    return cleaned


def _time_range(
    text: str,
    *,
    timezone_name: str,
    clock: Callable[[], datetime] | None,
) -> tuple[str | None, str | None]:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    local = now.astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)

    if "今天" in text or "今日" in text:
        return _iso(start), _iso(start + timedelta(days=1))
    if "昨天" in text:
        return _iso(start - timedelta(days=1)), _iso(start)
    if any(value in text for value in ("最近一周", "近一周", "最近7天", "近7天")):
        return _iso(local - timedelta(days=7)), _iso(local)
    if any(value in text for value in ("最近一个月", "近一个月", "最近30天", "近30天")):
        return _iso(local - timedelta(days=30)), _iso(local)
    if "上个月" in text or "上月" in text:
        first_this_month = start.replace(day=1)
        last_month_end = first_this_month
        last_month_start = (first_this_month - timedelta(days=1)).replace(day=1)
        return _iso(last_month_start), _iso(last_month_end)
    if "本月" in text or "这个月" in text:
        first_this_month = start.replace(day=1)
        if first_this_month.month == 12:
            next_month = first_this_month.replace(
                year=first_this_month.year + 1,
                month=1,
            )
        else:
            next_month = first_this_month.replace(month=first_this_month.month + 1)
        return _iso(first_this_month), _iso(next_month)
    return None, None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
