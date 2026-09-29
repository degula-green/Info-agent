"""answer.compose: organises evidence into a sourced answer.

The Runtime has no notion of an answer - it only knows steps and
observations - so the step that turns retrieved material into prose has to be
a capability like any other. It is read-only: it calls a model and returns
text, it never writes anywhere.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.kernel.models import CapabilityDescriptor

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
            "evidence 用 $steps.<step_id>.output.evidence 引用上游步骤的产出。"
        ),
        input_schema=AnswerComposeInput.model_json_schema(),
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
        draft = self.provider.compose(arguments.question, arguments.evidence)
        return AnswerComposeResult(
            answer=draft.answer,
            citations=keep_known_citations(draft.citations, arguments.evidence),
            # Reported so the Runtime can charge the Task budget: a capability
            # that spends model calls must not be able to spend them for free.
            model_calls=max(int(getattr(draft, "model_calls", 0) or 0), 0),
        ).model_dump()
