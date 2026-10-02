"""answer.compose: organises evidence into a sourced answer.

The Runtime has no notion of an answer - it only knows steps and
observations - so the step that turns retrieved material into prose has to be
a capability like any other. It is read-only: it calls a model and returns
text, it never writes anywhere.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kernel.models import (
    CapabilityDescriptor,
    CapabilityInputBinding,
    StepOutputRef,
)

CAPABILITY_NAME = "answer.compose"
KNOWLEDGE_SEARCH_CAPABILITY = "knowledge.search_content"
WEB_RESEARCH_CAPABILITY = "web.research"
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
    knowledge_evidence: list[dict[str, Any]] = Field(
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
    evidence_ref: StepOutputRef | None = Field(
        default=None,
        description="引用更早 web.research 步骤输出的 evidence（兼容旧计划）",
    )
    evidence_refs: list[StepOutputRef] = Field(
        default_factory=list,
        description="引用一个或多个更早 web.research 步骤输出的 evidence",
    )
    knowledge_evidence_refs: list[StepOutputRef] = Field(
        default_factory=list,
        description="引用一个或多个更早 knowledge.search_content 步骤输出的 evidence",
    )

    @model_validator(mode="after")
    def _evidence_required(self) -> "AnswerComposePlanInput":
        if not self.evidence_ref and not self.evidence_refs and not self.knowledge_evidence_refs:
            raise ValueError("at least one evidence reference is required")
        return self


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


class AnswerComposeCapability:
    """Calls the answer provider and validates the citations it returns."""

    descriptor = CapabilityDescriptor(
        name=CAPABILITY_NAME,
        description=(
            "依据已检索到的 evidence 组织一段带来源的回答（只读）。"
            "证据通过 evidence_refs / knowledge_evidence_refs 指向更早步骤输出的 evidence，"
            "支持网页和知识库等多来源证据合并。"
            "不需要自然语言回答的任务可以不调用本能力。"
        ),
        input_schema=AnswerComposeInput.model_json_schema(),
        planner_input_schema=AnswerComposePlanInput.model_json_schema(),
        input_bindings=[
            CapabilityInputBinding(
                planner_argument="evidence_ref",
                runtime_argument="evidence",
                source_capability=WEB_RESEARCH_CAPABILITY,
                source_output="evidence",
                required=False,
            ),
            CapabilityInputBinding(
                planner_argument="evidence_refs",
                runtime_argument="evidence",
                source_capability=WEB_RESEARCH_CAPABILITY,
                source_output="evidence",
                aggregate=True,
                required=False,
            ),
            CapabilityInputBinding(
                planner_argument="knowledge_evidence_refs",
                runtime_argument="knowledge_evidence",
                source_capability=KNOWLEDGE_SEARCH_CAPABILITY,
                source_output="evidence",
                aggregate=True,
                required=False,
            ),
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
        evidence = _merge_evidence(arguments.knowledge_evidence, arguments.evidence)
        draft = self.provider.compose(arguments.question, evidence)
        return AnswerComposeResult(
            answer=draft.answer,
            citations=keep_known_citations(draft.citations, evidence),
            # Reported so the Runtime can charge the Task budget: a capability
            # that spends model calls must not be able to spend them for free.
            model_calls=max(int(getattr(draft, "model_calls", 0) or 0), 0),
        ).model_dump()


def _merge_evidence(
    knowledge_evidence: list[dict[str, Any]],
    web_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*knowledge_evidence, *web_evidence]:
        if not isinstance(item, dict):
            continue
        identity = str(
            item.get("evidence_id")
            or item.get("content_hash")
            or item.get("url")
            or len(merged)
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)
        if len(merged) >= MAX_EVIDENCE_ITEMS:
            break
    return merged
