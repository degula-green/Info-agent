"""answer.compose: organises evidence into a sourced answer.

The Runtime has no notion of an answer - it only knows steps and
observations - so the step that turns retrieved material into prose has to be
a capability like any other. It is read-only: it calls a model and returns
text, it never writes anywhere.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.kernel.models import (
    CapabilityDescriptor,
    CapabilityInputBinding,
    StepOutputRef,
)
from app.kernel.execution_context import current_execution_context

CAPABILITY_NAME = "answer.compose"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_EVIDENCE_ITEMS = 50
QUOTE_CHARS = 500


class AnswerComposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    # Passed as a reference in the Plan: evidence="$steps.<id>.output.evidence".
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_ITEMS
    )


class AnswerComposePlanInput(BaseModel):
    """The Planner-facing shape of answer.compose.

    The Planner points at the step that produced the evidence instead of
    retyping it; the Runtime binder resolves that pointer into the list the
    capability reads. Making it required is the point: an answer written
    without its sources is not the work this step exists to do.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    evidence_ref: StepOutputRef = Field(
        description="引用更早 web.research 步骤输出的 evidence"
    )


class AnswerComposeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[dict[str, Any]]
    model_calls: int


def keep_known_citations(
    citations: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keeps only citations that point at evidence the step was given.

    A model that invents a source must not be able to put it in front of the
    user: the supplied evidence is the only set of ids that may appear.
    """

    known = {
        str(item.get("evidence_id"))
        for item in evidence
        if isinstance(item, dict) and item.get("evidence_id")
    }
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in citations or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id or evidence_id not in known or evidence_id in seen:
            continue
        seen.add(evidence_id)
        kept.append(
            {
                "evidence_id": evidence_id,
                "quote": str(item.get("quote") or "")[:QUOTE_CHARS],
            }
        )
    return kept


def _compose_with_context(provider, question: str, evidence: list[dict[str, Any]]):
    try:
        context = current_execution_context().conversation_context
    except RuntimeError:
        # Unit callers can exercise the capability directly; the kernel binds
        # this context for real executions.
        context = None
    if context is None:
        return provider.compose(question, evidence)
    signature = inspect.signature(provider.compose)
    if "conversation_context" not in signature.parameters and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return provider.compose(question, evidence)
    return provider.compose(
        question,
        evidence,
        conversation_context=context,
    )


class AnswerComposeCapability:
    """Calls the answer provider and validates the citations it returns."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description=(
            "依据已检索到的 evidence 组织一段带来源的回答（只读）。"
            "证据用 evidence_ref 指向更早 web.research 步骤输出的 evidence。"
            "不需要自然语言回答的任务可以不调用本能力。"
        ),
        input_schema=AnswerComposeInput.model_json_schema(),
        planner_input_schema=AnswerComposePlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="evidence_ref",
                runtime_argument="evidence",
                source_capability="web.research",
                source_output="evidence",
            )
        ],
        output_schema=AnswerComposeResult.model_json_schema(),
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )

    def __init__(self, provider, *, timeout_seconds: int | None = None) -> None:
        self.provider = provider
        if timeout_seconds is not None:
            self.descriptor = type(self).descriptor.model_copy(
                update={"timeout_seconds": int(timeout_seconds)}
            )

    def validate(self, arguments: dict[str, Any]) -> AnswerComposeInput:
        return AnswerComposeInput.model_validate(arguments)

    def execute(self, arguments: AnswerComposeInput) -> dict[str, Any]:
        draft = _compose_with_context(
            self.provider,
            arguments.question,
            arguments.evidence,
        )
        return AnswerComposeResult(
            answer=draft.answer,
            citations=keep_known_citations(draft.citations, arguments.evidence),
            # Reported so the Runtime can charge the Task budget: a capability
            # that spends model calls must not be able to spend them for free.
            model_calls=max(int(getattr(draft, "model_calls", 0) or 0), 0),
        ).model_dump()
