"""System One intent classification for the Laya sidecar and the Jev cloud API.

Both backends speak the same request/response protocol: a ``state`` plus named
typed ``questions``, answered with a chosen option, a probability distribution
and a confidence. They differ only in deployment, model id, thresholds, and the
field name carrying the confidence (Laya's local server returns
``answer_confidence``; Jev returns ``confidence``). This module owns the shared
logic so the two paths cannot drift apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.infrastructure.laya.client import LayaClient, LayaError
from app.kernel.models import TaskEnvelope, TaskUnderstanding, UnderstandingIntent
from app.understanding.schema import (
    BOUNDARY_INTENTS,
    INTENT_CATALOG,
    INTENT_NAMES,
    IntentDefinition,
    choice_criteria,
    intent_task_kind,
)

DEFAULT_MIN_CONFIDENCE = 0.80
DEFAULT_MIN_MARGIN = 0.15
# Kept as aliases: older callers and tests import the Laya-prefixed names.
DEFAULT_LAYAYA_MIN_CONFIDENCE = DEFAULT_MIN_CONFIDENCE
DEFAULT_LAYAYA_MIN_MARGIN = DEFAULT_MIN_MARGIN

# The multilingual checkpoint follows English option descriptions much more
# reliably than Chinese ones, so the criteria live in English (see schema.py).
_INSTRUCTION = "Choose the workflow that best matches the user's primary intent."


@dataclass(frozen=True)
class LayaEvaluation:
    understanding: TaskUnderstanding
    label: str
    answer_confidence: float
    margin: float
    probabilities: dict[str, float]
    accepted: bool
    fallback_reason: str | None = None


# A System One answer is the same shape whichever backend produced it.
SystemOneEvaluation = LayaEvaluation


class SystemOneUnderstandingProvider:
    """Classifies one TaskEnvelope into the Agent's intent catalog.

    ``name`` labels the backend in events (``laya`` / ``jev``); the protocol and
    the decision rules are identical. Only intents whose required capabilities
    are registered are offered to the model, so a deployment never asks for an
    intent it cannot execute.
    """

    model_backed = True
    estimated_model_calls = 1

    def __init__(
        self,
        client: LayaClient,
        *,
        name: str = "laya",
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
        available_intents: Iterable[IntentDefinition] | None = None,
    ) -> None:
        self.client = client
        self.name = str(name)
        self.model = f"{self.name}:{getattr(client, 'model', 'unknown')}"
        self.min_confidence = _clamp(min_confidence)
        self.min_margin = _clamp(min_margin)
        self.max_len = max_len
        self.head_max_len = head_max_len
        self.intents = (
            INTENT_CATALOG
            if available_intents is None
            else tuple(available_intents)
        )
        self.available_names = frozenset(item.name for item in self.intents)
        self.criteria = choice_criteria(self.intents)
        self.last_call_count = 0
        self.last_fallback_reason: str | None = None
        self.last_answer_confidence: float | None = None
        self.last_margin: float | None = None
        # The versioned model id the backend actually used, when it reports one.
        self.last_model: str | None = None

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
        self.last_model = None

        text = str(task.input.get("text") or "").strip()
        if not text:
            return LayaEvaluation(
                understanding=TaskUnderstanding(
                    is_task=False,
                    goal="",
                    confidence=1.0,
                    reason=f"{self.name}: empty input",
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
        # planner. The provider keeps its own stricter fast-path gate.
        threshold = max(self.min_confidence, runtime_threshold)
        questions = {
            "intent": {
                "type": "choice",
                "instructions": _INSTRUCTION,
                "criteria": self.criteria,
            }
        }

        self.last_call_count = 1
        raw = self.client.predict(
            # Keep the state minimal. Adding metadata such as source_type moves
            # the model's distribution substantially.
            {"text": text},
            questions,
            max_len=self.max_len,
            head_max_len=self.head_max_len,
        )
        self.last_model = _response_model(raw)
        label, answer_confidence, probabilities = _parse_choice(raw)
        ordered = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
        second_probability = ordered[1][1] if len(ordered) > 1 else 0.0
        margin = answer_confidence - second_probability

        self.last_answer_confidence = answer_confidence
        self.last_margin = margin

        if label not in INTENT_NAMES and label not in BOUNDARY_INTENTS:
            raise LayaError(f"{self.name} returned unknown intent: {label}")
        if label in INTENT_NAMES and label not in self.available_names:
            # The option was not offered, so an answer naming it is a protocol
            # error rather than a usable classification.
            raise LayaError(f"{self.name} returned unavailable intent: {label}")
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
        if label in self.available_names:
            competing = [
                name
                for name, probability in probabilities.items()
                if name in self.available_names and probability >= threshold
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
                    reason=f"{self.name}: {label}",
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
                    reason=f"{self.name}: other_task",
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
                reason=f"{self.name}: non_task",
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
                reason=f"{self.name} uncertain: {reason}",
            ),
            label=label,
            answer_confidence=answer_confidence,
            margin=margin,
            probabilities=probabilities,
            accepted=False,
            fallback_reason=reason,
        )


class LayaUnderstandingProvider(SystemOneUnderstandingProvider):
    """The Laya sidecar provider, kept as a named subclass for existing callers."""

    def __init__(
        self,
        client: LayaClient,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
        available_intents: Iterable[IntentDefinition] | None = None,
    ) -> None:
        super().__init__(
            client,
            name="laya",
            min_confidence=min_confidence,
            min_margin=min_margin,
            max_len=max_len,
            head_max_len=head_max_len,
            available_intents=available_intents,
        )


class JevUnderstandingProvider(SystemOneUnderstandingProvider):
    """The Jev cloud provider reached through AIHubMix / TypeSafe."""

    def __init__(
        self,
        client: LayaClient,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
        available_intents: Iterable[IntentDefinition] | None = None,
    ) -> None:
        super().__init__(
            client,
            name="jev",
            min_confidence=min_confidence,
            min_margin=min_margin,
            max_len=max_len,
            head_max_len=head_max_len,
            available_intents=available_intents,
        )


def _response_model(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_choice(raw: Mapping[str, Any]) -> tuple[str, float, dict[str, float]]:
    answers = raw.get("answers")
    if not isinstance(answers, Mapping):
        raise LayaError("System One response has no answers object")
    answer = answers.get("intent")
    if not isinstance(answer, Mapping):
        raise LayaError("System One response has no intent answer")

    label = answer.get("choice")
    if not isinstance(label, str) or not label.strip():
        raise LayaError("System One intent answer has no choice")

    raw_probabilities = answer.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or not raw_probabilities:
        raise LayaError("System One intent answer has no probabilities")
    probabilities: dict[str, float] = {}
    for name, value in raw_probabilities.items():
        if not isinstance(name, str) or not name:
            raise LayaError("System One intent probabilities contain an invalid name")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LayaError("System One intent probabilities contain a non-number")
        number = float(value)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            raise LayaError("System One intent probability is outside [0, 1]")
        probabilities[name] = number
    if label not in probabilities:
        raise LayaError("System One choice is missing from its probability distribution")

    # Laya's local server calls it answer_confidence; Jev's cloud API calls it
    # confidence and derives it from the distribution. Never fall back to the
    # top probability: those two numbers are not the same quantity.
    raw_confidence = answer.get("confidence")
    if raw_confidence is None:
        raw_confidence = answer.get("answer_confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise LayaError("System One answer_confidence is not a number")
    answer_confidence = float(raw_confidence)
    if not math.isfinite(answer_confidence) or not 0.0 <= answer_confidence <= 1.0:
        raise LayaError("System One answer_confidence is outside [0, 1]")
    return label.strip(), answer_confidence, probabilities


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


__all__ = [
    "DEFAULT_LAYAYA_MIN_CONFIDENCE",
    "DEFAULT_LAYAYA_MIN_MARGIN",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_MARGIN",
    "JevUnderstandingProvider",
    "LayaEvaluation",
    "LayaUnderstandingProvider",
    "SystemOneEvaluation",
    "SystemOneUnderstandingProvider",
]
