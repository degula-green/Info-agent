"""Wire schema and intent catalog for the task understanding provider.

The single source of truth for the intent taxonomy is ``intent_contract.json``
next to this module. The Agent, the Laya provider, the training script and the
dataset generator all read that file (or validate against it), so option names
and their order cannot drift apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.kernel.models import TaskUnderstanding, UnderstandingIntent

CONTRACT_PATH = Path(__file__).with_name("intent_contract.json")

TaskKind = Literal["answer", "action", "mixed"]
VALID_TASK_KINDS = frozenset({"answer", "action", "mixed"})
VALID_OPTION_KINDS = frozenset({"business", "boundary"})


@dataclass(frozen=True)
class IntentDefinition:
    name: str
    description: str
    examples: tuple[str, ...]
    task_kind: TaskKind | None
    laya_criteria: str
    generation_prompt: str
    kind: str


def _load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - packaging error
        raise RuntimeError(f"intent contract not found: {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("intent contract must be a JSON object")

    version = raw.get("schema_version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("intent contract has no schema_version")

    options = raw.get("options")
    if not isinstance(options, list) or not options:
        raise RuntimeError("intent contract has no options")

    seen: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise RuntimeError("intent contract option must be an object")
        name = option.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("intent contract option has no name")
        if name in seen:
            raise RuntimeError(f"intent contract has a duplicate option: {name}")
        seen.add(name)
        if option.get("kind") not in VALID_OPTION_KINDS:
            raise RuntimeError(f"intent contract option {name} has an invalid kind")
        task_kind = option.get("task_kind")
        if task_kind is not None and task_kind not in VALID_TASK_KINDS:
            raise RuntimeError(f"intent contract option {name} has an invalid task_kind")
        for field_name in ("description", "laya_criteria", "generation_prompt"):
            value = option.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(
                    f"intent contract option {name} has no {field_name}"
                )
    return raw


CONTRACT: Mapping[str, Any] = _load_contract()
INTENT_SCHEMA_VERSION: str = str(CONTRACT["schema_version"])

INTENT_OPTION_ORDER: tuple[str, ...] = tuple(
    option["name"] for option in CONTRACT["options"]
)

INTENT_DEFINITIONS: tuple[IntentDefinition, ...] = tuple(
    IntentDefinition(
        name=option["name"],
        description=option["description"],
        examples=tuple(option.get("examples") or ()),
        task_kind=option.get("task_kind"),
        laya_criteria=option["laya_criteria"],
        generation_prompt=option["generation_prompt"],
        kind=option["kind"],
    )
    for option in CONTRACT["options"]
)

# The LLM prompt only offers the five business intents; the two boundary labels
# are described separately as hard constraints.
INTENT_CATALOG: tuple[IntentDefinition, ...] = tuple(
    item for item in INTENT_DEFINITIONS if item.kind == "business"
)

INTENT_NAMES = frozenset(item.name for item in INTENT_CATALOG)

BOUNDARY_INTENTS = frozenset(
    item.name for item in INTENT_DEFINITIONS if item.kind == "boundary"
)

ALL_INTENT_LABELS = frozenset(INTENT_OPTION_ORDER)

INTENT_TASK_KINDS: dict[str, TaskKind] = {
    item.name: item.task_kind  # type: ignore[misc]
    for item in INTENT_DEFINITIONS
    if item.task_kind is not None
}

LAYAYA_CRITERIA: dict[str, str] = {
    item.name: item.laya_criteria for item in INTENT_DEFINITIONS
}

LAYAYA_INSTRUCTION: str = str(CONTRACT["instruction"])


def generation_prompt(name: str) -> str:
    for item in INTENT_DEFINITIONS:
        if item.name == name:
            return item.generation_prompt
    raise KeyError(f"unknown intent: {name}")


class UnderstandingIntentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1)
    evidence: str | None = Field(default=None, max_length=500)


class TaskUnderstandingDraft(BaseModel):
    """The exact JSON object that an LLM provider must return."""

    model_config = ConfigDict(extra="forbid")

    is_task: bool
    goal: str = Field(min_length=1, max_length=1000)
    task_kind: Literal["answer", "action", "mixed"] | None = None
    intent_candidates: list[UnderstandingIntentDraft] = Field(
        default_factory=list, max_length=8
    )
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("intent_candidates")
    @classmethod
    def _known_intents(
        cls, value: list[UnderstandingIntentDraft]
    ) -> list[UnderstandingIntentDraft]:
        unknown = sorted({item.name for item in value if item.name not in INTENT_NAMES})
        if unknown:
            raise ValueError(
                "unknown intent candidates: "
                + ", ".join(unknown)
                + "; allowed values: "
                + ", ".join(sorted(INTENT_NAMES))
            )
        return value

    @model_validator(mode="after")
    def _non_task_has_no_intents(self) -> "TaskUnderstandingDraft":
        if not self.is_task and self.intent_candidates:
            raise ValueError("is_task=false cannot contain intent_candidates")
        return self

    def to_understanding(self) -> TaskUnderstanding:
        candidates = sorted(
            self.intent_candidates,
            key=lambda item: item.confidence,
            reverse=True,
        )
        return TaskUnderstanding(
            is_task=self.is_task,
            goal=self.goal,
            task_kind=self.task_kind,
            intent_candidates=[
                UnderstandingIntent(
                    name=item.name,
                    confidence=item.confidence,
                    evidence=item.evidence,
                )
                for item in candidates
            ],
            confidence=self.confidence,
            reason=self.reason,
        )


def intent_catalog_text() -> str:
    return "\n".join(
        f"- {item.name}: {item.description}; examples: {'; '.join(item.examples)}"
        for item in INTENT_CATALOG
    )


def intent_task_kind(name: str) -> TaskKind:
    return INTENT_TASK_KINDS.get(name, "action")
