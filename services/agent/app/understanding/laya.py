"""System One intent classification for the Laya sidecar and the Jev cloud API.

Both backends answer the same ``state`` + typed ``questions`` request, so they
share one provider and one set of decision rules. The intent contract stays the
single source of truth for the option set: a fine-tuned Laya checkpoint is
positionally bound to it (see :func:`verify_model_contract`), so the criteria
are never filtered per deployment.
"""

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

EVIDENCE_PLAN_NAME = "evidence_plan"
# Below this the source verdict is treated as "not judged" and the planner
# applies its own default. It is deliberately looser than the intent gate: the
# question only has three options, so a modest confidence still carries signal.
EVIDENCE_PLAN_MIN_CONFIDENCE = 0.5

# The three answers map straight onto the source lists the planner consumes.
EVIDENCE_PLAN_SOURCES: dict[str, tuple[str, ...]] = {
    "attachment_only": ("attachment",),
    "knowledge_only": ("knowledge",),
    "attachment_and_knowledge": ("attachment", "knowledge"),
}

_EVIDENCE_PLAN_INSTRUCTION = (
    "判断回答这条消息需要哪些证据来源。"
    "用户明确要求只用附件、或问题明确限定在附件内容范围内时，选 attachment_only。"
    "用户只问公司内部资料、且消息没有附件时，选 knowledge_only。"
    "只要消息带有附件，而指令没有明确排除附件，就选 attachment_and_knowledge："
    "回答可能需要公司资料补充，指令也没有限定来源，宁可多查也不要漏查。"
)

_EVIDENCE_PLAN_CRITERIA: dict[str, str] = {
    "attachment_only": (
        "The user explicitly wants the answer taken from the uploaded attachment "
        "alone, or the question is clearly scoped to the attachment's contents."
    ),
    "knowledge_only": (
        "The user asks only about company or internal knowledge and the message "
        "carries no attachment."
    ),
    "attachment_and_knowledge": (
        "The message carries an attachment and the instruction does not rule it "
        "out, so the answer may need both the attachment and company knowledge. "
        "Prefer this whenever the instruction is ambiguous about sources."
    ),
}

_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
)


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


def build_question(
    *, include_evidence_plan: bool = False
) -> dict[str, dict[str, Any]]:
    """The typed questions sent to System One.

    ``include_evidence_plan`` is off by default because the fine-tuned Laya
    checkpoint is positionally bound to the intent question; only the cloud
    backend is asked for the extra source decision.
    """

    questions: dict[str, dict[str, Any]] = {
        "intent": {
            "type": "choice",
            "instructions": LAYAYA_INSTRUCTION,
            "criteria": _ordered_criteria(),
        }
    }
    if include_evidence_plan:
        questions[EVIDENCE_PLAN_NAME] = {
            "type": "choice",
            "instructions": _EVIDENCE_PLAN_INSTRUCTION,
            "criteria": dict(_EVIDENCE_PLAN_CRITERIA),
        }
    return questions


def build_evidence_state(task: TaskEnvelope, text: str) -> dict[str, Any]:
    """State for the source decision: the instruction plus what attachments exist.

    The attachment *bodies* stay out on purpose. The question is which sources
    the instruction calls for, and a long document would drown the very wording
    that carries that signal.
    """

    state: dict[str, Any] = {"text": text}
    attachment_ids = task.input.get("attachment_ids") or []
    if not attachment_ids:
        return state
    has_text = bool(str(task.input.get("_attachment_excerpt") or "").strip())
    attachments: list[dict[str, Any]] = []
    for name in task.input.get("_attachment_file_names") or []:
        file_name = str(name).strip()
        if not file_name:
            continue
        attachments.append(
            {
                "name": file_name,
                "kind": (
                    "image"
                    if Path(file_name).suffix.lower() in _IMAGE_SUFFIXES
                    else "document"
                ),
                "has_text": has_text,
            }
        )
    if not attachments:
        attachments = [
            {"name": "attachment", "kind": "document", "has_text": has_text}
        ]
    state["attachments"] = attachments
    return state


def parse_evidence_plan(
    raw: Mapping[str, Any],
) -> tuple[list[str], float, dict[str, float]]:
    """Read the source answer, tolerating a backend that does not return one."""

    answers = raw.get("answers")
    if not isinstance(answers, Mapping):
        return [], 0.0, {}
    answer = answers.get(EVIDENCE_PLAN_NAME)
    if not isinstance(answer, Mapping):
        return [], 0.0, {}
    sources = EVIDENCE_PLAN_SOURCES.get(str(answer.get("choice") or "").strip())
    if sources is None:
        return [], 0.0, {}

    raw_confidence = answer.get("confidence")
    if raw_confidence is None:
        raw_confidence = answer.get("answer_confidence")
    if isinstance(raw_confidence, bool) or not isinstance(
        raw_confidence, (int, float)
    ):
        confidence = 0.0
    else:
        confidence = float(raw_confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            confidence = 0.0

    probabilities: dict[str, float] = {}
    raw_probabilities = answer.get("probabilities")
    if isinstance(raw_probabilities, Mapping):
        for name, value in raw_probabilities.items():
            if (
                isinstance(name, str)
                and name
                and not isinstance(value, bool)
                and isinstance(value, (int, float))
            ):
                probabilities[name] = float(value)
    return list(sources), confidence, probabilities


def score_text(
    client: LayaClient,
    text: str,
    *,
    max_len: int | None = None,
    head_max_len: int | None = None,
) -> tuple[str, float, dict[str, float]]:
    """Raw option scores for one text using the frozen contract question."""

    raw = client.predict(
        # Keep the state minimal. Adding metadata such as source_type moves the
        # stock multilingual checkpoint's distribution substantially.
        {"text": text},
        build_question(),
        max_len=max_len,
        head_max_len=head_max_len,
    )
    return _interpret(raw, source="Laya")


def _interpret(
    raw: Mapping[str, Any],
    *,
    source: str = "System One",
) -> tuple[str, float, dict[str, float]]:
    """Validate one raw response against the frozen intent contract."""

    label, answer_confidence, probabilities = _parse_choice(raw, source=source)
    unknown = sorted(name for name in probabilities if name not in ALL_INTENT_LABELS)
    if unknown:
        raise LayaError(
            f"{source} returned probabilities for unknown intents: "
            + ", ".join(unknown)
        )
    if label not in ALL_INTENT_LABELS:
        raise LayaError(f"{source} returned unknown intent: {label}")
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


class SystemOneUnderstandingProvider:
    """Classifies one TaskEnvelope into the Agent's fixed intent catalog.

    ``name`` labels the backend in events (``laya`` / ``jev``). The protocol and
    the decision rules are identical; only the endpoint, the thresholds and the
    name of the confidence field differ between the two backends.
    """

    model_backed = True
    estimated_model_calls = 1
    schema_version = INTENT_SCHEMA_VERSION

    def __init__(
        self,
        client: LayaClient,
        *,
        name: str = "laya",
        min_confidence: float = DEFAULT_LAYAYA_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_LAYAYA_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
        request_evidence_plan: bool = False,
    ) -> None:
        self.client = client
        self.name = str(name)
        self.model = f"{self.name}:{getattr(client, 'model', 'unknown')}"
        self.min_confidence = _clamp(min_confidence)
        self.min_margin = _clamp(min_margin)
        self.max_len = max_len
        self.head_max_len = head_max_len
        # Only the cloud backend is asked to judge evidence sources. The local
        # Laya checkpoint is fine-tuned for the intent question alone.
        self.request_evidence_plan = bool(request_evidence_plan)
        self.last_call_count = 0
        self.last_fallback_reason: str | None = None
        self.last_answer_confidence: float | None = None
        self.last_margin: float | None = None
        # The versioned model id the backend reported, when it reports one.
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
        # planner. Laya keeps its own stricter fast-path gate.
        threshold = max(self.min_confidence, runtime_threshold)
        self.last_call_count = 1
        # Only the cloud backend gets the source question, and with it the
        # attachment inventory. The local checkpoint keeps the minimal state it
        # was trained on: extra metadata moves its distribution substantially.
        if self.request_evidence_plan:
            state: dict[str, Any] = build_evidence_state(task, text)
            questions = build_question(include_evidence_plan=True)
        else:
            state = {"text": text}
            questions = build_question()
        raw = self.client.predict(
            state,
            questions,
            max_len=self.max_len,
            head_max_len=self.head_max_len,
        )
        self.last_model = _response_model(raw)
        label, answer_confidence, probabilities = _interpret(raw, source=self.name)
        evidence_sources, evidence_reason = self._resolve_evidence_plan(raw)
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
                evidence_sources,
                evidence_reason,
            )
        if margin < self.min_margin:
            return self._uncertain(
                text,
                label,
                answer_confidence,
                margin,
                probabilities,
                "low_margin",
                evidence_sources,
                evidence_reason,
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
                    evidence_sources,
                    evidence_reason,
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
                    evidence_sources=evidence_sources,
                    evidence_reason=evidence_reason,
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
                    evidence_sources=evidence_sources,
                    evidence_reason=evidence_reason,
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
                evidence_sources=evidence_sources,
                evidence_reason=evidence_reason,
            ),
            label=label,
            answer_confidence=answer_confidence,
            margin=margin,
            probabilities=probabilities,
            accepted=True,
        )

    def _resolve_evidence_plan(
        self, raw: Mapping[str, Any]
    ) -> tuple[list[str], str | None]:
        """The judged sources, or empty when the question was not asked/answered."""

        if not self.request_evidence_plan:
            return [], None
        sources, confidence, _ = parse_evidence_plan(raw)
        if not sources:
            return [], f"{self.name}: no source verdict"
        if confidence < EVIDENCE_PLAN_MIN_CONFIDENCE:
            return [], f"{self.name}: source verdict below threshold"
        return sources, f"{self.name}: {'+'.join(sources)}"

    def _uncertain(
        self,
        text: str,
        label: str,
        answer_confidence: float,
        margin: float,
        probabilities: dict[str, float],
        reason: str,
        evidence_sources: list[str] | None = None,
        evidence_reason: str | None = None,
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
                evidence_sources=list(evidence_sources or []),
                evidence_reason=evidence_reason,
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
        min_confidence: float = DEFAULT_LAYAYA_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_LAYAYA_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
    ) -> None:
        super().__init__(
            client,
            name="laya",
            min_confidence=min_confidence,
            min_margin=min_margin,
            max_len=max_len,
            head_max_len=head_max_len,
        )


class JevUnderstandingProvider(SystemOneUnderstandingProvider):
    """The Jev cloud provider reached through AIHubMix / TypeSafe."""

    def __init__(
        self,
        client: LayaClient,
        *,
        min_confidence: float = DEFAULT_LAYAYA_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_LAYAYA_MIN_MARGIN,
        max_len: int | None = None,
        head_max_len: int | None = None,
    ) -> None:
        super().__init__(
            client,
            name="jev",
            min_confidence=min_confidence,
            min_margin=min_margin,
            max_len=max_len,
            head_max_len=head_max_len,
            # The cloud backend can answer the extra source question; the
            # fine-tuned local checkpoint cannot.
            request_evidence_plan=True,
        )


def _response_model(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_choice(
    raw: Mapping[str, Any],
    *,
    source: str = "System One",
) -> tuple[str, float, dict[str, float]]:
    answers = raw.get("answers")
    if not isinstance(answers, Mapping):
        raise LayaError(f"{source} response has no answers object")
    answer = answers.get("intent")
    if not isinstance(answer, Mapping):
        raise LayaError(f"{source} response has no intent answer")

    label = answer.get("choice")
    if not isinstance(label, str) or not label.strip():
        raise LayaError(f"{source} intent answer has no choice")

    raw_probabilities = answer.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or not raw_probabilities:
        raise LayaError(f"{source} intent answer has no probabilities")
    probabilities: dict[str, float] = {}
    for name, value in raw_probabilities.items():
        if not isinstance(name, str) or not name:
            raise LayaError(f"{source} intent probabilities contain an invalid name")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LayaError(f"{source} intent probabilities contain a non-number")
        number = float(value)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            raise LayaError(f"{source} intent probability is outside [0, 1]")
        probabilities[name] = number
    if label not in probabilities:
        raise LayaError(
            f"{source} choice is missing from its probability distribution"
        )

    # Laya's local server calls it answer_confidence; Jev's cloud API calls it
    # confidence and derives it from the distribution. Never fall back to the
    # top probability: those two numbers are not the same quantity.
    raw_confidence = answer.get("confidence")
    if raw_confidence is None:
        raw_confidence = answer.get("answer_confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise LayaError(f"{source} answer confidence is not a number")
    answer_confidence = float(raw_confidence)
    if not math.isfinite(answer_confidence) or not 0.0 <= answer_confidence <= 1.0:
        raise LayaError(f"{source} answer confidence is outside [0, 1]")
    return label.strip(), answer_confidence, probabilities


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
