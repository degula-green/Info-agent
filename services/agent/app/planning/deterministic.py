"""Deterministic planner: collected text in, at most one to-do step out.

Pure function: no LLM. It reuses the ingress pre-filter vocabulary so the entry
check and the planning decision can never drift apart.

The planner is deliberately single step. Tasks that need several capabilities
composed into a sequence (search -> OCR -> fill a form) belong to the LLM
planner, which receives the whole Capability Catalog and returns a step list.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.timeparse import parse_time_expression
from app.ingress.vocabulary import (
    TIME_PHRASE_PATTERN,
    chitchat_only,
    task_candidate_hint,
)
from app.understanding.prompt import DEFAULT_MIN_CONFIDENCE
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

TITLE_LIMIT = 30
DEFAULT_TITLE = "待办"
# The single intent this planner understands, and the capability it maps to.
# They share a name here because both describe one product object, but the two
# are still named separately: an intent names what the user wants, a capability
# names what the system can do, and nothing requires them to be the same string.
INTENT_NAME = "todo.create"
DEFAULT_CAPABILITY_NAME = "todo.create"
DEFAULT_MIN_CONFIDENCE = DEFAULT_MIN_CONFIDENCE
_TRIM = " \t\r\n，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》…~-_"

# A bare "下周" / "早上" carries a due hint without a resolvable moment. It is
# not a deadline the Agent can commit to, so it is only stripped from the title.
_COARSE_TIME = re.compile(
    r"(?:今天|今日|明天|明日|后天|大后天|今晚|明晚|今早"
    r"|(?:下下|下|这|本)?(?:周|星期|礼拜)[一二三四五六日天]"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|凌晨|早上|上午|中午|下午|傍晚|晚上"
    r"|(?:这|本|下)\s*个?\s*(?:周|星期|月))"
)


def capability_idempotency_key(task: TaskEnvelope) -> str:
    """Business key of the external write: one to-do per message and owner.

    It is composed from the source instead of the Plan or Step so a re-plan
    (user supplied the missing time) reuses the very same request_id.
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


def extract_time_expression(text: str) -> tuple[str, str | None]:
    """The maximal time phrase in the text, or nothing when there is none."""

    match = TIME_PHRASE_PATTERN.search(str(text or ""))
    if match is None:
        return "", None
    return match.group(0).strip(), match.group(0)


def build_title(text: str, matched: str | None) -> str:
    """The to-do title: the message without its time phrase or filler words."""

    remainder = text.replace(matched, " ", 1) if matched else text
    remainder = _COARSE_TIME.sub(" ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip(_TRIM).strip()
    if not remainder:
        return DEFAULT_TITLE
    return remainder[:TITLE_LIMIT]


class DeterministicPlanner:
    def __init__(
        self,
        *,
        default_timezone: str = "Asia/Shanghai",
        capability_name: str = DEFAULT_CAPABILITY_NAME,
        clock: Callable[[], datetime] | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self.default_timezone = default_timezone
        self.capability_name = capability_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        plan_id = str(uuid4())
        text = str(task.input.get("text") or "").strip()
        registered = {descriptor.name for descriptor in capabilities}
        steps: list[PlanStep] = []
        objective = "没有需要执行的动作"
        unsupported: list[str] = []
        warnings: list[str] = []
        requires_confirmation = False

        wants_todo = False
        if understanding is not None:
            # Candidates arrive sorted by confidence; the threshold keeps a
            # low-confidence guess from turning into a real draft, and only the
            # highest scoring hit is acted on so a weak second guess cannot
            # widen the plan.
            accepted = [
                item
                for item in understanding.intent_candidates
                if item.confidence >= self.min_confidence
            ]
            best = accepted[0].name if accepted else None
            wants_todo = bool(understanding.is_task and best == INTENT_NAME)
            unsupported = [item.name for item in accepted if item.name != best]
            if best is not None and best != INTENT_NAME:
                unsupported.append(best)
            if wants_todo and self.capability_name not in registered:
                # The intent is understood but cannot be executed right now.
                unsupported.append(INTENT_NAME)
                wants_todo = False
            if not wants_todo:
                if understanding.intent_candidates:
                    objective = "当前没有可执行的意图"
                return Plan(
                    plan_id=plan_id,
                    task_id=task.task_id,
                    objective=objective,
                    steps=[],
                    unsupported_intents=unsupported,
                    warnings=[f"unsupported intent: {name}" for name in unsupported],
                )
        else:
            # No Understanding (``AGENT_UNDERSTANDING_MODE=off``): fall back to the
            # same gate the ingress uses, so "完成登录模块代码" -- which has no time
            # and no schedule keyword -- is still planned instead of dropped.
            wants_todo = bool(
                text and task_candidate_hint(text) and not chitchat_only(text)
            )

        if wants_todo and self.capability_name in registered:
            expression, matched = extract_time_expression(text)
            title = build_title(text, matched)
            objective = f"创建待办：{title}"
            arguments: dict[str, Any] = {
                "title": title,
                # The phrase travels with the Step even when it is empty: the
                # capability owns resolving it, and an empty one simply means
                # "no due time", which is a legitimate to-do.
                "due_expression": expression,
                "timezone": self.default_timezone,
                "owner_user_id": task.owner_user_id,
                "idempotency_key": capability_idempotency_key(task),
                "source": self._source(task),
            }
            # Resolve the phrase now so the approval shows the moment that will
            # actually be written. An ambiguous phrase stays unresolved: the
            # to-do is still created, only without a due time.
            due_at = self._resolve_due(expression, task)
            if due_at is not None:
                arguments["due_at"] = due_at
            steps.append(
                PlanStep(
                    step_id=f"{plan_id}-step-1",
                    plan_id=plan_id,
                    order=1,
                    capability=self.capability_name,
                    arguments=arguments,
                )
            )
            capability_descriptor = next(
                (
                    descriptor
                    for descriptor in capabilities
                    if descriptor.name == self.capability_name
                ),
                None,
            )
            if unsupported:
                warnings = [f"unsupported intent: {name}" for name in unsupported]
                requires_confirmation = bool(
                    capability_descriptor is not None
                    and capability_descriptor.side_effect
                    and not task.input.get("partial_execution_confirmation")
                )

        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective=objective,
            steps=steps,
            unsupported_intents=unsupported,
            warnings=warnings,
            requires_user_confirmation=requires_confirmation,
        )

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        latest = observations[-1] if observations else None
        if latest is not None and latest.status != "succeeded":
            classification = str((latest.error or {}).get("classification") or "")
            return PlannerDecision(
                action="fail",
                reason=f"deterministic planner cannot recover from {classification or 'failed capability'}",
            )
        if latest is not None and latest.output and latest.output.get("requires_user_input"):
            return PlannerDecision(
                action="request_input",
                required_input=list(latest.output.get("missing_information") or []),
                reason="capability requires user input",
            )
        if any(step.status in {"pending", "ready", "running"} for step in current_plan.steps):
            return PlannerDecision(action="continue")
        if not current_plan.steps and understanding is not None:
            if not understanding.is_task:
                return PlannerDecision(
                    action="complete",
                    reason=understanding.reason or "not a task",
                )
            unsupported = list(current_plan.unsupported_intents)
            if unsupported:
                return PlannerDecision(
                    action="unsupported",
                    unsupported_intents=unsupported,
                    warnings=list(current_plan.warnings)
                    or [f"unsupported intent: {name}" for name in unsupported],
                    requires_user_confirmation=current_plan.requires_user_confirmation,
                    reason="no registered capability can satisfy the candidate intents",
                )
        return PlannerDecision(
            action="complete",
            warnings=list(current_plan.warnings),
        )

    @staticmethod
    def _source(task: TaskEnvelope) -> dict[str, Any]:
        source_ref = task.source_ref or {}
        return {
            key: source_ref.get(key)
            for key in (
                "knowledge_item_id",
                "content_version",
                "conversation_type",
                "sender_display_name",
                "sent_at",
            )
            if source_ref.get(key) is not None
        }

    def _resolve_due(self, expression: str, task: TaskEnvelope) -> str | None:
        """Best-effort ISO due time for the collected phrase.

        The capability re-resolves the same expression before writing, so this
        only makes the pending approval readable ("2026-09-27 12:00" instead of
        "明天上午12点") and never becomes a second source of truth. Returning
        nothing is a normal outcome: the to-do simply has no due time yet.
        """

        if not expression:
            return None
        try:
            zone = ZoneInfo(self.default_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return None
        base = self.clock()
        sent_at = str((task.source_ref or {}).get("sent_at") or "").strip()
        if sent_at:
            try:
                parsed_base = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
            except ValueError:
                parsed_base = None
            if parsed_base is not None and parsed_base.tzinfo is not None:
                base = parsed_base
        if base.tzinfo is None:
            return None
        resolved = parse_time_expression(expression, base, zone)
        if resolved is None:
            return None
        return resolved.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
