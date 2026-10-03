"""Deterministic routing for questions about the current conversation.

Memory recall is Agent state, not internal-document knowledge. These phrases
must not be sent to the knowledge retriever, where "记住了吗?" can only produce
"没有找到满足条件的内容".
"""

from __future__ import annotations

import re
from uuid import uuid4

from app.capabilities.chat_reply import (
    CAPABILITY_NAME as CHAT_REPLY_CAPABILITY,
    MAX_TEXT_CHARS,
)
from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanStep,
    TaskEnvelope,
)

MEMORY_RECALL_OBJECTIVE = "使用当前会话记忆回答"

_RECALL_MARKERS = (
    "记住了吗",
    "记住吗",
    "还记得",
    "记不记得",
    "刚才我说",
    "之前我说",
    "我说过什么",
    "我是谁",
    "我叫什么",
    "我的名字叫什么",
)
_REMEMBER_COMMAND = re.compile(r"^(?:请|帮我)?记住(?:一下|住)?[，,:：\s]+")
_STRONG_SELF_INTRO = re.compile(
    r"^(?:我叫|我的名字(?:是|叫))\s*([\w\u4e00-\u9fff·-]{1,40})"
)
_WEAK_SELF_INTRO = re.compile(r"^我是\s*([\w\u4e00-\u9fff·-]{2,20})")


def is_conversation_memory_request(text: str) -> bool:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return False
    if any(marker in normalized for marker in _RECALL_MARKERS):
        return True
    if _REMEMBER_COMMAND.match(normalized):
        return True
    return bool(
        _STRONG_SELF_INTRO.match(normalized)
        or _WEAK_SELF_INTRO.match(normalized)
    )


def build_conversation_memory_plan(
    task: TaskEnvelope,
    capabilities: list[CapabilityDescriptor],
) -> Plan | None:
    if str(getattr(task, "source_type", "") or "") != "chat":
        return None
    task_input = getattr(task, "input", {}) or {}
    text = str(task_input.get("_original_text") or task_input.get("text") or "").strip()
    if not text or not is_conversation_memory_request(text):
        return None
    registered = {descriptor.name for descriptor in capabilities}
    if CHAT_REPLY_CAPABILITY not in registered:
        return None

    plan_id = str(uuid4())
    return Plan(
        plan_id=plan_id,
        task_id=task.task_id,
        objective=MEMORY_RECALL_OBJECTIVE,
        steps=[
            PlanStep(
                step_id=f"{plan_id}-step-1",
                plan_id=plan_id,
                order=1,
                capability=CHAT_REPLY_CAPABILITY,
                arguments={"text": text[:MAX_TEXT_CHARS]},
            )
        ],
    )


def is_conversation_memory_plan(plan: Plan) -> bool:
    return (
        plan.objective == MEMORY_RECALL_OBJECTIVE
        and len(plan.steps) == 1
        and plan.steps[0].capability == CHAT_REPLY_CAPABILITY
    )


def decide_conversation_memory_plan(
    plan: Plan,
    observations: list[Observation],
) -> PlannerDecision | None:
    if not is_conversation_memory_plan(plan):
        return None
    latest = observations[-1] if observations else None
    if latest is None:
        return PlannerDecision(
            action="continue",
            reason="等待当前会话记忆回复",
        )
    if latest.status == "succeeded":
        return PlannerDecision(
            action="complete",
            reason="当前会话记忆回复已生成",
        )
    if latest.status == "failed":
        return PlannerDecision(
            action="fail",
            reason="当前会话记忆回复失败",
        )
    return PlannerDecision(
        action="continue",
        reason="等待当前会话记忆回复",
    )


__all__ = [
    "MEMORY_RECALL_OBJECTIVE",
    "build_conversation_memory_plan",
    "decide_conversation_memory_plan",
    "is_conversation_memory_plan",
    "is_conversation_memory_request",
]
