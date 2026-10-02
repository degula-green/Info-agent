"""Laya multilingual intent classification."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.infrastructure.laya.client import LayaClient, LayaError
from app.kernel.models import TaskEnvelope, TaskUnderstanding, UnderstandingIntent
from app.understanding.schema import (
    ALL_INTENT_LABELS,
    BOUNDARY_INTENTS,
    INTENT_NAMES,
    INTENT_OPTION_ORDER,
    INTENT_SCHEMA_VERSION,
    LAYAYA_CRITERIA,
    LAYAYA_INSTRUCTION,
    intent_task_kind,
)

DEFAULT_LAYAYA_MIN_CONFIDENCE = 0.80
DEFAULT_LAYAYA_MIN_MARGIN = 0.15


def _ordered_criteria() -> dict[str, str]:
    """Criteria in the frozen contract order; option position is the model target."""

    ordered = {name: LAYAYA_CRITERIA[name] for name in INTENT_OPTION_ORDER}
    if set(ordered) != set(LAYAYA_CRITERIA):
        raise RuntimeError("Laya criteria do not match the intent contract options")
    return ordered


def verify_model_contract(model_dir: str | Path) -> dict[str, Any]:
    """Refuse a checkpoint that was fine-tuned for a different intent contract.

    Called during application start-up when ``AGENT_LAYAYA_MODEL_PATH`` points
    at a local checkpoint, so a contract mismatch surfaces immediately instead
    of on the first request.
    """

    config_path = Path(model_dir) / "rl_agent_config.json"
    if not config_path.exists():
        raise LayaError(f"Laya model config not found: {config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LayaError(f"Laya model config is not valid JSON: {config_path}") from exc

    version = config.get("intent_schema_version")
    if version != INTENT_SCHEMA_VERSION:
        raise LayaError(
            f"Laya model schema version {version!r} does not match the Agent "
            f"contract {INTENT_SCHEMA_VERSION!r}: {config_path}"
        )
    order = tuple(config.get("option_order") or ())
    if order != INTENT_OPTION_ORDER:
        raise LayaError(
            f"Laya model option order {list(order)} does not match the Agent "
            f"contract {list(INTENT_OPTION_ORDER)}: {config_path}"
        )
    return config


def build_question() -> dict[str, dict[str, Any]]:
    return {
        "intent": {
            "type": "choice",
            "instructions": LAYAYA_INSTRUCTION,
            "criteria": _ordered_criteria(),
        }
    }


def score_text(
    client: LayaClient,
    text: str,
    *,
    max_len: int | None = None,
    head_max_len: int | None = None,
) -> tuple[str, float, dict[str, float]]:
    """Raw option scores for one text, before any confidence gating."""

    raw = client.predict(
        # Keep the state minimal. Adding metadata such as source_type moves the
        # stock multilingual checkpoint's distribution substantially.
        {"text": text},
        build_question(),
        max_len=max_len,
        head_max_len=head_max_len,
    )
    label, answer_confidence, probabilities = _parse_choice(raw)
    unknown = sorted(name for name in probabilities if name not in ALL_INTENT_LABELS)
    if unknown:
        raise LayaError(
            "Laya returned probabilities for unknown intents: " + ", ".join(unknown)
        )
    if label not in ALL_INTENT_LABELS:
        raise LayaError(f"Laya returned unknown intent: {label}")
    return label, answer_confidence, probabilities


@dataclass(frozen=True)
class LayaEvaluation:
    understanding: TaskUnderstanding
    label: str
    answer_confidence: float
    margin: float
    probabilities: dict[str, float]
    accepted: bool
    fallback_reason: str | None = None
    schema_version: str = INTENT_SCHEMA_VERSION


class LayaUnderstandingProvider:
    """Classifies one TaskEnvelope into the Agent's fixed intent catalog."""

    name = "laya"
    model_backed = True
    estimated_model_calls = 1
    schema_version = INTENT_SCHEMA_VERSION

    def __init__(
        self,
        client: LayaClient,
        *,
        min_confidence: float = DEFAULT_LAYAYA_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_LAYAYA_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
    ) -> None:
        self.client = client
        self.model = f"laya:{getattr(client, 'model', 'unknown')}"
        self.min_confidence = _clamp(min_confidence)
        self.min_margin = _clamp(min_margin)
        self.max_len = max_len
        self.head_max_len = head_max_len
        self.last_call_count = 0
        self.last_fallback_reason: str | None = None
        self.last_answer_confidence: float | None = None
        self.last_margin: float | None = None

    def understand(
        self,
        task: TaskEnvelope,
        *,
        min_confidence: float | None = None,
    ) -> TaskUnderstanding:
        return self.evaluate(task, min_confidence=min_confidence).understanding

    def evaluate(
        self,
        task: TaskEnvelope,
        *,
        min_confidence: float | None = None,
    ) -> LayaEvaluation:
        self.last_call_count = 0
        self.last_fallback_reason = None
        self.last_answer_confidence = None
        self.last_margin = None

        text = str(task.input.get("text") or "").strip()
        if not text:
            return LayaEvaluation(
                understanding=TaskUnderstanding(
                    is_task=False,
                    goal="",
                    confidence=1.0,
                    reason="laya: empty input",
                ),
                label="non_task",
                answer_confidence=1.0,
                margin=1.0,
                probabilities={"non_task": 1.0},
                accepted=True,
            )

        runtime_threshold = (
            self.min_confidence
            if min_confidence is None
            else _clamp(min_confidence)
        )
        # Runtime thresholds describe the minimum confidence exposed to the
        # planner. Laya keeps its own stricter fast-path gate.
        threshold = max(self.min_confidence, runtime_threshold)
        self.last_call_count = 1
        label, answer_confidence, probabilities = score_text(
            self.client,
            text,
            max_len=self.max_len,
            head_max_len=self.head_max_len,
        )
        ordered = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
        second_probability = ordered[1][1] if len(ordered) > 1 else 0.0
        margin = answer_confidence - second_probability

        self.last_answer_confidence = answer_confidence
        self.last_margin = margin

        if answer_confidence < threshold:
            return self._uncertain(
                text,
                label,
                answer_confidence,
                margin,
                probabilities,
                "low_probability",
            )
        if margin < self.min_margin:
            return self._uncertain(
                text,
                label,
                answer_confidence,
                margin,
                probabilities,
                "low_margin",
            )
        if label in INTENT_NAMES:
            competing = [
                name
                for name, probability in probabilities.items()
                if name in INTENT_NAMES and probability >= threshold
            ]
            if len(competing) > 1:
                return self._uncertain(
                    text,
                    label,
                    answer_confidence,
                    margin,
                    probabilities,
                    "multiple_intents",
                )
            return LayaEvaluation(
                understanding=TaskUnderstanding(
                    is_task=True,
                    goal=text,
                    task_kind=intent_task_kind(label),
                    intent_candidates=[
                        UnderstandingIntent(
                            name=label,
                            confidence=answer_confidence,
                            evidence=None,
                        )
                    ],
                    confidence=answer_confidence,
                    reason=f"laya: {label}",
                ),
                label=label,
                answer_confidence=answer_confidence,
                margin=margin,
                probabilities=probabilities,
                accepted=True,
            )
        if label == "other_task":
            return LayaEvaluation(
                understanding=TaskUnderstanding(
                    is_task=True,
                    goal=text,
                    task_kind="action",
                    intent_candidates=[],
                    confidence=answer_confidence,
                    reason="laya: other_task",
                ),
                label=label,
                answer_confidence=answer_confidence,
                margin=margin,
                probabilities=probabilities,
                accepted=True,
            )
        return LayaEvaluation(
            understanding=TaskUnderstanding(
                is_task=False,
                goal=text,
                task_kind=None,
                intent_candidates=[],
                confidence=answer_confidence,
                reason="laya: non_task",
            ),
            label=label,
            answer_confidence=answer_confidence,
            margin=margin,
            probabilities=probabilities,
            accepted=True,
        )

    def _uncertain(
        self,
        text: str,
        label: str,
        answer_confidence: float,
        margin: float,
        probabilities: dict[str, float],
        reason: str,
    ) -> LayaEvaluation:
        self.last_fallback_reason = reason
        return LayaEvaluation(
            understanding=TaskUnderstanding(
                # An uncertain non_task must not silently drop a possible task.
                is_task=True,
                goal=text,
                task_kind=None,
                intent_candidates=[],
                confidence=answer_confidence,
                reason=f"laya uncertain: {reason}",
            ),
            label=label,
            answer_confidence=answer_confidence,
            margin=margin,
            probabilities=probabilities,
            accepted=False,
            fallback_reason=reason,
        )


def _parse_choice(raw: Mapping[str, Any]) -> tuple[str, float, dict[str, float]]:
    answers = raw.get("answers")
    if not isinstance(answers, Mapping):
        raise LayaError("Laya response has no answers object")
    answer = answers.get("intent")
    if not isinstance(answer, Mapping):
        raise LayaError("Laya response has no intent answer")

    label = answer.get("choice")
    if not isinstance(label, str) or not label.strip():
        raise LayaError("Laya intent answer has no choice")

    raw_probabilities = answer.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or not raw_probabilities:
        raise LayaError("Laya intent answer has no probabilities")
    probabilities: dict[str, float] = {}
    for name, value in raw_probabilities.items():
        if not isinstance(name, str) or not name:
            raise LayaError("Laya intent probabilities contain an invalid name")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LayaError("Laya intent probabilities contain a non-number")
        number = float(value)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            raise LayaError("Laya intent probability is outside [0, 1]")
        probabilities[name] = number
    if label not in probabilities:
        raise LayaError("Laya choice is missing from its probability distribution")

    raw_confidence = answer.get("answer_confidence", probabilities[label])
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise LayaError("Laya answer_confidence is not a number")
    answer_confidence = float(raw_confidence)
    if not math.isfinite(answer_confidence) or not 0.0 <= answer_confidence <= 1.0:
        raise LayaError("Laya answer_confidence is outside [0, 1]")
    return label.strip(), answer_confidence, probabilities


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
