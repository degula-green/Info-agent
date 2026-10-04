"""Deterministic routing for company knowledge questions.

The LLM planner is useful for ambiguous, multi-step tasks, but knowledge
retrieval is too important to depend on a model choosing the right tools every
time. This module recognizes the stable knowledge intents and emits a fixed
Capability plan before the configured planner gets a chance to drift.
"""

from __future__ import annotations

import hashlib
import inspect
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
from app.capabilities.answer import CAPABILITY_NAME as ANSWER_COMPOSE_NAME
from app.capabilities.web_research import CAPABILITY_NAME as WEB_RESEARCH_NAME
from app.infrastructure.web.url_tools import extract_http_urls
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
from app.planning.conversation_memory import (
    build_conversation_memory_plan,
    decide_conversation_memory_plan,
)

KnowledgeMode = Literal["sources", "content", "content_with_sources"]

COMPLIANCE_ASSESS_INTENT = "compliance.assess"
COMPLIANCE_OBJECTIVE = "对照外部协议评估公司材料"
FORM_COMPLETE_INTENT = "form.complete"
FORM_PREVIEW_NAME = "form.preview"
FORM_APPLY_NAME = "form.apply"
_NO_SUBMIT_MARKERS = (
    "不要提交",
    "不用提交",
    "先不提交",
    "不提交",
    "别提交",
    "仅填写",
    "只填写",
)
_COMPLIANCE_WEB_MARKERS = (
    "协议",
    "标准",
    "法规",
    "条例",
    "规范",
    "网上",
    "公网",
    "互联网",
    "公开信息",
    "新闻",
    "最新",
)
_COMPLIANCE_CONTEXT_TERMS = (
    "公司 制度 流程 数据 安全 合规 服务器 账号 密码 合同 部署"
)

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
    "在聊什么",
    "聊什么",
    "聊了什么",
    "聊了些",
    "讨论什么",
    "讨论了什么",
    "都说了什么",
    "说了些什么",
    "什么话题",
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
    "哪个平台",
    "什么平台",
    "在哪个软件",
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
_INTERNAL_OBJECT_MARKERS = (
    "官网",
    "项目",
    "系统",
    "平台",
    "服务器",
    "部署",
    "上线",
    "任务",
    "需求",
    "功能",
    "模块",
    "接口",
    "环境",
    "测试",
    "开发",
    "进度",
    "阶段",
)
_STATUS_MARKERS = (
    "哪个阶段",
    "什么阶段",
    "现阶段",
    "当前阶段",
    "当前在",
    "现在在",
    "什么情况",
    "现在怎么样",
    "进展",
    "进度",
    "状态",
    "到哪了",
    "到哪一步",
    "完成了吗",
    "上线了吗",
    "部署了吗",
    "什么时候上线",
    "何时上线",
    "卡在哪",
    "还差什么",
    "负责人",
    "谁负责",
    "谁部署",
    "谁做",
    "在哪台服务器",
    "部署到哪",
)
_WEB_MARKERS = (
    "网上",
    "公网",
    "互联网",
    "百度",
    "搜索引擎",
    "公开信息",
    "官网公告",
    "官网上的",
    "最新公告",
    "新闻",
    "链接",
    "网址",
    "url",
    "http://",
    "https://",
)
_CHITCHAT = {"你好", "您好", "hello", "hi", "谢谢", "多谢", "再见", "在吗"}
_PERSONAL_CONTEXT_MARKERS = (
    "我的",
    "我们的",
    "我们公司",
    "本公司",
    "我司",
    "公司简介",
    "已采集",
    "知识库",
    "内部资料",
)
_URL_PATTERN = re.compile(r"https?://[^\s，。；、]+", re.IGNORECASE)

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
    # Human-readable window already resolved from "昨天"/"上周" etc. It travels
    # with the plan so the answer step never recomputes relative dates.
    time_range: str | None = None
    # The user's timezone, so evidence timestamps can be rendered locally.
    timezone: str | None = None


def classify_knowledge_question(
    text: str,
    *,
    timezone_name: str = "Asia/Shanghai",
    clock: Callable[[], datetime] | None = None,
) -> KnowledgeRoute | None:
    question = " ".join(str(text or "").split()).strip()
    query = question[:500]
    normalized = question.lower().strip(" \t\r\n，。,.!！?？、:：;；")
    if not normalized or normalized in _CHITCHAT:
        return None

    has_content = _contains_any(normalized, _CONTENT_MARKERS)
    has_source = _contains_any(normalized, _SOURCE_MARKERS)
    has_search = _contains_any(normalized, _SEARCH_MARKERS)
    has_file = _contains_any(normalized, _FILE_MARKERS)
    has_message = _contains_any(normalized, _MESSAGE_MARKERS)
    has_action = _contains_any(normalized, _ACTION_MARKERS)
    has_internal_object = _contains_any(normalized, _INTERNAL_OBJECT_MARKERS)
    has_status = _contains_any(normalized, _STATUS_MARKERS)
    has_explicit_web = _contains_any(normalized, _WEB_MARKERS)
    has_internal_status = has_internal_object and has_status

    if has_explicit_web:
        return None

    if has_action and not (has_source or has_content):
        return None
    # An internal entity on its own ("青云官网", "帮我查一下青云官网") is a
    # knowledge lookup, not a web lookup: the explicit-public wording that would
    # make it web.research already returned None above. Without this the text
    # falls through to the LLM planner, which has no search tool and improvises
    # a web.fetch against a guessed URL.
    if not (
        has_source
        or has_content
        or has_internal_status
        or has_internal_object
        or (has_search and (has_file or has_message))
    ):
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
    content_arguments: dict[str, Any] = {"query": query}

    mode: KnowledgeMode
    if has_internal_status:
        route_to_sources = bool(
            sender_names
            or conversation_names
            or has_file
            or has_message
        )
        mode = "content_with_sources" if route_to_sources else "content"
    elif has_content and (
        has_source or sender_names or conversation_names or occurred_after
    ):
        # A content question that names a sender, a conversation or a time range
        # is answered from the filtered sources; a BM25 pass over the whole
        # sentence would drown the filter words in the query.
        mode = "content_with_sources"
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
        query=query,
        source_query=source_query,
        source_arguments={key: value for key, value in source_arguments.items() if value not in (None, "", [], False)},
        content_arguments=content_arguments,
        objective=objective,
        time_range=_format_window(
            occurred_after, occurred_before, timezone_name
        ),
        timezone=timezone_name,
    )


def _format_window(
    after: str | None,
    before: str | None,
    timezone_name: str,
) -> str | None:
    if not after and not before:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc

    def render(value: str | None) -> str:
        if not value:
            return "?"
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")

    return f"{render(after)} 至 {render(before)} ({timezone_name})"


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
    sources_step: PlanStep | None = None
    if route.mode == "sources":
        if SEARCH_SOURCES_NAME not in registered:
            return None
        sources_step = add_step(SEARCH_SOURCES_NAME, dict(route.source_arguments))
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
        sources_step = add_step(SEARCH_SOURCES_NAME, dict(route.source_arguments))
        search_content_step = add_step(
            SEARCH_CONTENT_NAME,
            {
                **route.content_arguments,
                "resource_ids_ref": {"step": 1, "output": "resource_ids"},
                "restrict_to_resource_ids": True,
            },
        )

    reference: dict[str, Any] | None = None
    if search_content_step is not None:
        reference = {
            "results_ref": {
                "step": search_content_step.order,
                "output": "results",
            },
            "metadata_coverage_ref": {
                "step": search_content_step.order,
                "output": "metadata_coverage",
            },
        }
    elif sources_step is not None:
        # A metadata question ("which platform is this group on") is answered
        # from the source records, which carry fields the content endpoint
        # does not expose.
        reference = {
            "sources_ref": {
                "step": sources_step.order,
                "output": "sources",
            },
            "metadata_coverage_ref": {
                "step": sources_step.order,
                "output": "metadata_coverage",
            },
        }

    if reference is not None and KNOWLEDGE_ANSWER_NAME in registered:
        answer_arguments: dict[str, Any] = {
            "query": route.content_arguments["query"],
            **reference,
        }
        if route.time_range:
            answer_arguments["time_range"] = route.time_range
        if route.timezone:
            answer_arguments["timezone"] = route.timezone
        add_step(
            KNOWLEDGE_ANSWER_NAME,
            answer_arguments,
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
        *,
        conversation_context=None,
    ) -> Plan:
        memory_plan = build_conversation_memory_plan(task, capabilities)
        if memory_plan is not None:
            self._last_call_count = 0
            return memory_plan
        if _explicit_web_research(understanding):
            # The classifier can name a public-web intent with high confidence
            # ("搜索一下飞书开放平台"). The generic internal-object words
            # ("平台"/"系统") must not override that: doing so turns a web
            # lookup into an internal search that has no matching evidence.
            built = _call_plan(
                self.base.create_plan,
                task,
                capabilities,
                observations,
                constraints,
                understanding,
                conversation_context=conversation_context,
            )
            self._last_call_count = max(
                int(getattr(self.base, "last_call_count", 0) or 0),
                0,
            )
            return augment_personal_knowledge_sources(
                built,
                task,
                capabilities,
            )
        if _requests_compliance_assessment(understanding) or _requests_document_comparison(
            task
        ):
            compliance_plan = build_compliance_plan(
                task,
                capabilities,
            )
            if compliance_plan is not None:
                self._last_call_count = 0
                return compliance_plan
        if _explicit_action_task(task, understanding):
            built = _call_plan(
                self.base.create_plan,
                task,
                capabilities,
                observations,
                constraints,
                understanding,
                conversation_context=conversation_context,
            )
            self._last_call_count = max(
                int(getattr(self.base, "last_call_count", 0) or 0),
                0,
            )
            return augment_personal_knowledge_sources(
                built,
                task,
                capabilities,
            )
        if _requests_form_completion(understanding):
            form_plan = build_form_plan(task, capabilities)
            if form_plan is not None:
                self._last_call_count = 0
                return form_plan
        route = classify_knowledge_question(
            _task_instruction_text(task),
            timezone_name=self.default_timezone,
            clock=self.clock,
        )
        if route is not None:
            plan = build_knowledge_plan(route, task, capabilities)
            if plan is not None:
                self._last_call_count = 0
                return plan
        built = _call_plan(
            self.base.create_plan,
            task,
            capabilities,
            observations,
            constraints,
            understanding,
            conversation_context=conversation_context,
        )
        built = augment_personal_knowledge_sources(
            built,
            task,
            capabilities,
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
        *,
        conversation_context=None,
    ) -> PlannerDecision:
        memory_decision = decide_conversation_memory_plan(
            current_plan,
            observations,
        )
        if memory_decision is not None:
            self._last_call_count = 0
            return memory_decision
        if _is_knowledge_plan(current_plan):
            self._last_call_count = 0
            return _decide_knowledge_plan(current_plan, observations)
        latest = observations[-1] if observations else None
        if (
            latest is not None
            and latest.status == "succeeded"
            and _is_composite_source_plan(current_plan)
            and any(
                step.status in {"pending", "ready", "running"}
                for step in current_plan.steps
            )
        ):
            # The initial plan already contains all required source and answer
            # steps. A model replan after the first success must not silently
            # discard the remaining source or the final composition step.
            self._last_call_count = 0
            return PlannerDecision(
                action="continue",
                reason="当前计划仍有可执行的后续步骤",
            )
        decision = _call_plan(
            self.base.decide_after_observation,
            task,
            current_plan,
            observations,
            constraints,
            understanding,
            conversation_context=conversation_context,
        )
        self._last_call_count = max(
            int(getattr(self.base, "last_call_count", 0) or 0),
            0,
        )
        return decision


def _call_plan(call, *args, conversation_context=None):
    signature = inspect.signature(call)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs or "conversation_context" in signature.parameters:
        return call(*args, conversation_context=conversation_context)
    return call(*args)


def augment_personal_knowledge_sources(
    plan: Plan,
    task: TaskEnvelope,
    capabilities: list[CapabilityDescriptor],
) -> Plan:
    """Add the user's own knowledge as a source for personal comparisons.

    The LLM planner is free to choose any plan. This post-pass only enforces a
    generic data-boundary rule: when the instruction explicitly refers to the
    user's own/company/internal data and also asks the Agent to read public web
    content before a final answer, the plan must include a private-knowledge
    retrieval step. It is not tied to any domain intent.
    """

    text = _task_instruction_text(task)
    normalized = text.lower()
    if not _contains_any(normalized, _PERSONAL_CONTEXT_MARKERS):
        return plan
    if not _URL_PATTERN.search(text):
        return plan

    registered = {descriptor.name for descriptor in capabilities}
    if SEARCH_CONTENT_NAME not in registered:
        return plan

    web_step = next(
        (step for step in plan.steps if step.capability == WEB_RESEARCH_NAME),
        None,
    )
    answer_step = next(
        (step for step in plan.steps if step.capability == ANSWER_COMPOSE_NAME),
        None,
    )
    if web_step is None or answer_step is None:
        return plan

    knowledge_step = next(
        (step for step in plan.steps if step.capability == SEARCH_CONTENT_NAME),
        None,
    )
    if knowledge_step is None:
        original = [
            step.model_copy(deep=True)
            for step in sorted(plan.steps, key=lambda item: item.order)
        ]
        knowledge_step = PlanStep(
            step_id=f"{plan.plan_id}-step-1",
            plan_id=plan.plan_id,
            order=1,
            capability=SEARCH_CONTENT_NAME,
            arguments={
                "query": _personal_context_query(text),
                "include_personal": True,
            },
        )
        shifted: list[PlanStep] = [knowledge_step]
        for step in original:
            step.order += 1
            step.step_id = f"{plan.plan_id}-step-{step.order}"
            step.arguments = _shift_reference_steps(step.arguments)
            shifted.append(step)
        plan.steps = shifted
        answer_step = next(
            (step for step in shifted if step.capability == ANSWER_COMPOSE_NAME),
            None,
        )
    else:
        # A model-authored knowledge step often repeats the user's question
        # verbatim ("我的公司是否符合标准"). That query has little lexical
        # overlap with the factual document it needs to retrieve, so replace it
        # with the same deterministic attribute query used when adding a step.
        knowledge_step.arguments["query"] = _personal_context_query(text)
        knowledge_step.arguments["include_personal"] = True

    if answer_step is not None:
        # The model may have written an empty literal list here. Removing it is
        # required because the runtime rejects mixing a value and its ref.
        answer_step.arguments.pop("knowledge_evidence", None)
        answer_step.arguments["knowledge_evidence_refs"] = [
            {"step": knowledge_step.order, "output": "evidence"}
        ]
    return plan


def _personal_context_query(text: str) -> str:
    query = _URL_PATTERN.sub(" ", text)
    query = " ".join(query.split()).strip()
    if _contains_any(query.lower(), ("公司", "企业", "估值", "融资", "上市")):
        return f"{query} 公司简介 成立时间 估值 融资 上市状态"[:500]
    return f"{query} 个人知识库 相关事实 数据"[:500]


def _task_instruction_text(task: TaskEnvelope) -> str:
    """The user's own sentence, never the merged attachment body."""

    original = str(task.input.get("_original_text") or "").strip()
    if original:
        return original
    return str(task.input.get("text") or "").strip()


def _shift_reference_steps(value: Any) -> Any:
    if isinstance(value, list):
        return [_shift_reference_steps(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"step", "output"} and isinstance(value.get("step"), int):
            return {**value, "step": value["step"] + 1}
        return {key: _shift_reference_steps(item) for key, item in value.items()}
    return value


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
    # answer.compose is the generic composition step. An LLM-authored knowledge
    # plan may end with it instead of knowledge.answer, and that plan is just as
    # deterministic to close out: after the retrieval succeeded the only work
    # left is the answer step. Without this the runtime paid for a second LLM
    # decision on a result that needed none.
    knowledge = {
        SEARCH_SOURCES_NAME,
        SEARCH_CONTENT_NAME,
        KNOWLEDGE_ANSWER_NAME,
        ANSWER_COMPOSE_NAME,
    }
    return all(step.capability in knowledge for step in plan.steps)


def build_compliance_plan(
    task: TaskEnvelope,
    capabilities: list[CapabilityDescriptor],
) -> Plan | None:
    """Compose the comparison the compliance intent actually asks for.

    ``compliance.assess`` has no single capability: the work is retrieve the
    company's own material, read the external protocol, and compare them. When
    those capabilities are registered the deterministic pipeline can build the
    whole plan without a model guessing at step shapes.
    """

    registered = {descriptor.name for descriptor in capabilities}
    if ANSWER_COMPOSE_NAME not in registered:
        return None
    text = _task_instruction_text(task)
    attachment_excerpt = str(task.input.get("_attachment_excerpt") or "").strip()
    has_attachment = bool(attachment_excerpt)
    has_knowledge = SEARCH_CONTENT_NAME in registered and not has_attachment
    urls = extract_http_urls(text)
    has_web = WEB_RESEARCH_NAME in registered and (
        bool(urls) or _contains_any(text.lower(), _COMPLIANCE_WEB_MARKERS)
    )
    if not has_knowledge and not has_web:
        return None

    plan_id = str(uuid4())
    steps: list[PlanStep] = []
    knowledge_step: PlanStep | None = None
    web_step: PlanStep | None = None
    if has_knowledge:
        knowledge_step = PlanStep(
            step_id=f"{plan_id}-step-{len(steps) + 1}",
            plan_id=plan_id,
            order=len(steps) + 1,
            capability=SEARCH_CONTENT_NAME,
            arguments={
                "query": _compliance_query(text),
                "include_personal": True,
            },
        )
        steps.append(knowledge_step)
    if has_web:
        web_step = PlanStep(
            step_id=f"{plan_id}-step-{len(steps) + 1}",
            plan_id=plan_id,
            order=len(steps) + 1,
            capability=WEB_RESEARCH_NAME,
            arguments={
                "request": text,
                "urls": urls,
                "queries": [] if urls else [_compliance_web_query(text)],
            },
        )
        steps.append(web_step)

    answer_arguments: dict[str, Any] = {"question": text[:2000]}
    if has_attachment:
        answer_arguments["knowledge_evidence"] = [
            _attachment_evidence(task, attachment_excerpt)
        ]
    if web_step is not None:
        answer_arguments["evidence_refs"] = [
            {"step": web_step.order, "output": "evidence"}
        ]
    if knowledge_step is not None:
        answer_arguments["knowledge_evidence_refs"] = [
            {"step": knowledge_step.order, "output": "evidence"}
        ]
    steps.append(
        PlanStep(
            step_id=f"{plan_id}-step-{len(steps) + 1}",
            plan_id=plan_id,
            order=len(steps) + 1,
            capability=ANSWER_COMPOSE_NAME,
            arguments=answer_arguments,
        )
    )
    return Plan(
        plan_id=plan_id,
        task_id=task.task_id,
        objective=COMPLIANCE_OBJECTIVE,
        steps=steps,
    )


def build_form_plan(
    task: TaskEnvelope,
    capabilities: list[CapabilityDescriptor],
) -> Plan | None:
    """The fixed preview -> apply pipeline for a user-provided form URL."""

    registered = {descriptor.name for descriptor in capabilities}
    if FORM_PREVIEW_NAME not in registered or FORM_APPLY_NAME not in registered:
        return None
    text = _task_instruction_text(task)
    urls = extract_http_urls(text)
    if not urls:
        return None
    plan_id = str(uuid4())
    wants_submit = "提交" in text and not any(
        marker in text for marker in _NO_SUBMIT_MARKERS
    )
    action = "fill_and_submit" if wants_submit else "fill_only"
    return Plan(
        plan_id=plan_id,
        task_id=task.task_id,
        objective="填写并提交表单" if wants_submit else "填写表单",
        steps=[
            PlanStep(
                step_id=f"{plan_id}-step-1",
                plan_id=plan_id,
                order=1,
                capability=FORM_PREVIEW_NAME,
                arguments={"request": text, "url": urls[0]},
            ),
            PlanStep(
                step_id=f"{plan_id}-step-2",
                plan_id=plan_id,
                order=2,
                capability=FORM_APPLY_NAME,
                arguments={
                    "request": text,
                    "draft_ref": {"step": 1, "output": "form"},
                    "action": action,
                },
            ),
        ],
    )


def _compliance_query(text: str) -> str:
    """A broad company-fact query, independent of the external document."""

    subject = " ".join(_URL_PATTERN.sub(" ", str(text or "")).split())[:120]
    return f"{subject} {_COMPLIANCE_CONTEXT_TERMS}".strip()[:500]


def _compliance_web_query(text: str) -> str:
    """The protocol name, without the "combine with our material" clause."""

    value = str(text or "")
    for marker in (
        "然后结合",
        "并结合",
        "请结合",
        "结合我们",
        "我们公司",
        "请说明",
        "并说明",
        "哪些符合",
    ):
        index = value.find(marker)
        if index > 0:
            value = value[:index]
            break
    return " ".join(value.split())[:200] or " ".join(str(text or "").split())[:200]


def _attachment_evidence(task: TaskEnvelope, excerpt: str) -> dict[str, Any]:
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    file_names = [
        str(item)
        for item in (task.input.get("_attachment_file_names") or [])
        if str(item).strip()
    ]
    return {
        "evidence_id": f"att-{digest[:16]}",
        "source_type": "document",
        "title": file_names[0] if file_names else "附件",
        "url": None,
        "quote": excerpt[:500],
        "text": excerpt,
        "content_hash": digest,
        "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fetch_method": "attachment",
        "version": 1,
    }


def _explicit_web_research(understanding: TaskUnderstanding | None) -> bool:
    """Whether the classifier named public-web research with real confidence.

    Only a confident candidate counts: an uncertain one still falls through to
    the deterministic internal-knowledge route, which is the guard for
    company-owned names such as "青云官网".
    """

    if understanding is None or not understanding.is_task:
        return False
    return any(
        item.name == WEB_RESEARCH_NAME and item.confidence >= 0.9
        for item in understanding.intent_candidates
    )


def _requests_compliance_assessment(
    understanding: TaskUnderstanding | None,
) -> bool:
    if understanding is None or not understanding.is_task:
        return False
    return any(
        item.name == COMPLIANCE_ASSESS_INTENT and item.confidence >= 0.7
        for item in understanding.intent_candidates
    )


def _requests_form_completion(
    understanding: TaskUnderstanding | None,
) -> bool:
    if understanding is None or not understanding.is_task:
        return False
    return any(
        item.name == FORM_COMPLETE_INTENT and item.confidence >= 0.7
        for item in understanding.intent_candidates
    )


def _requests_document_comparison(task: TaskEnvelope) -> bool:
    """An uploaded document plus an external link plus a compliance verdict."""

    if not task.input.get("attachment_ids"):
        return False
    text = _task_instruction_text(task)
    if not extract_http_urls(text):
        return False
    return any(
        marker in text
        for marker in ("符合", "不符合", "对比", "比较", "是否", "合规", "评估")
    )


def _explicit_action_task(
    task: TaskEnvelope,
    understanding: TaskUnderstanding | None,
) -> bool:
    """An unlabelled but clear action phrase belongs to the action planner."""

    if understanding is None or not understanding.is_task:
        return False
    if understanding.intent_candidates:
        return False
    text = _task_instruction_text(task)
    if any(
        marker in text
        for marker in ("是什么", "什么是", "怎么", "如何", "为什么", "是否", "哪些")
    ):
        return False
    if any(
        marker in text
        for marker in (
            "表单",
            "表格",
            "填表",
            "填写",
            "预览",
            "上传",
            "附件",
            "网址",
            "http://",
            "https://",
            "协议",
            "对比",
            "比较",
            "搜索",
            "查询",
            "检索",
            "总结",
            "分析",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "完成",
            "写",
            "提交",
            "开发",
            "修复",
            "处理",
            "准备",
            "整理",
            "买",
            "取",
            "做",
            "安排",
            "跟进",
            "联系",
            "发送",
            "回复",
            "提醒",
            "创建",
            "新建",
        )
    )


def _is_composite_source_plan(plan: Plan) -> bool:
    capabilities = {step.capability for step in plan.steps}
    return (
        SEARCH_CONTENT_NAME in capabilities
        and WEB_RESEARCH_NAME in capabilities
        and ANSWER_COMPOSE_NAME in capabilities
    )


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
        # "昨天晚上10点aims群" carries the time phrase in front of the group
        # name; keeping it would filter for a conversation that does not exist.
        name = _LEADING_TIME_PREFIX.sub("", name).strip()
        for candidate in _conversation_candidates(name):
            if candidate and candidate not in names:
                names.append(candidate)
    return names[:10]


def _conversation_candidates(name: str) -> tuple[str, ...]:
    """The typed phrase plus the bare name it wraps ("aims群" -> "aims").

    Collected conversations are stored under the bare name while the user
    types the generic suffix ("群里"), so filtering on the typed phrase alone
    matched nothing. Both forms are offered; a filter simply gains one more
    acceptable name.
    """

    stripped = re.sub(r"(?:群聊|群|频道)$", "", name).strip()
    if stripped and stripped != name:
        return (stripped, name)
    return (name,)


_LEADING_TIME_PREFIX = re.compile(
    r"^(?:(?:今天|今日|昨天|昨晚|前天|明天|后天|本周|这周|上周"
    r"|本月|这个月|上个月|最近|近)"
    r"(?:早上|上午|中午|下午|晚上|凌晨)?"
    r"[\s\d点:：半年月日号周星期礼拜]*)+"
)


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
