"""Deterministic step-2 planner: collected text in, at most one calendar step out.

Pure function: no LLM, no ``TaskUnderstandingProvider``, no Calendar specific
Builder. It reuses the ingress pre-filter vocabulary so the entry check and the
planning decision can never drift apart.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from app.capabilities.calendar import CAPABILITY_NAME, extract_time_expression
from app.ingress.vocabulary import schedule_hint
from app.kernel.models import CapabilityDescriptor, Observation, Plan, PlanStep, TaskEnvelope

TITLE_LIMIT = 30
DEFAULT_TITLE = "日程"
_TRIM = " \t\r\n，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》…~-_"


def capability_idempotency_key(task: TaskEnvelope) -> str:
    """Business key of the external write: one draft per message and owner.

    It is composed from the source instead of the Plan or Step so a re-plan
    (user supplied the missing time) reuses the very same ``request_id``.
    """

    source_ref = task.source_ref or {}
    if task.source_type == "knowledge_event":
        item = str(
            source_ref.get("knowledge_item_id") or source_ref.get("source_message_id") or ""
        ).strip()
        if item:
            version = source_ref.get("content_version")
            return f"knowledge_event:{task.owner_user_id}:{item}:{version}"
    return f"chat:{task.owner_user_id}:{task.task_id}"


def build_title(text: str, matched: str | None) -> str:
    remainder = text.replace(matched, " ", 1) if matched else text
    remainder = re.sub(r"\s+", " ", remainder).strip(_TRIM).strip()
    if not remainder:
        return DEFAULT_TITLE
    return remainder[:TITLE_LIMIT]


class DeterministicPlanner:
    def __init__(
        self,
        *,
        default_timezone: str = "Asia/Shanghai",
        capability_name: str = CAPABILITY_NAME,
    ) -> None:
        self.default_timezone = default_timezone
        self.capability_name = capability_name

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
    ) -> Plan:
        plan_id = str(uuid4())
        text = str(task.input.get("text") or "").strip()
        registered = {descriptor.name for descriptor in capabilities}
        steps: list[PlanStep] = []
        objective = "没有需要执行的动作"

        if text and self.capability_name in registered and schedule_hint(text):
            expression, matched = extract_time_expression(text)
            title = build_title(text, matched)
            objective = f"创建日程：{title}"
            steps.append(
                PlanStep(
                    step_id=f"{plan_id}-step-1",
                    plan_id=plan_id,
                    order=1,
                    capability=self.capability_name,
                    arguments={
                        "title": title,
                        "time_expression": expression,
                        "timezone": self.default_timezone,
                        "owner_user_id": task.owner_user_id,
                        "idempotency_key": capability_idempotency_key(task),
                        "source": self._source(task),
                    },
                )
            )

        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective=objective,
            steps=steps,
        )

    @staticmethod
    def _source(task: TaskEnvelope) -> dict[str, Any]:
        source_ref = task.source_ref or {}
        return {
            key: source_ref.get(key)
            for key in ("knowledge_item_id", "content_version", "conversation_type", "sent_at")
            if source_ref.get(key) is not None
        }
