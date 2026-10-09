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

SUBJECT_KIND_NAME = "subject_kind"
QUERY_FOCUS_NAME = "query_focus"
SUBJECT_KIND_VALUES = frozenset({"person", "organization", "none"})
QUERY_FOCUS_VALUES = frozenset(
    {"person_self", "work_object", "general_knowledge", "other"}
)
# These are deliberately looser than the intent threshold: the auxiliary
# questions have fewer options and are used for arbitration, not for choosing
# every intent by themselves.
SUBJECT_FOCUS_MIN_CONFIDENCE = 0.70

_SUBJECT_KIND_INSTRUCTION = (
    "判断这条消息指向的主体类型。昵称、别名、拟人化称呼也算 person；"
    "公司、团队、部门、学校、机构算 organization；"
    "项目、系统、文档、任务不是主体人物。"
)
_SUBJECT_KIND_CRITERIA: dict[str, str] = {
    "person": (
        "The message is about a specific person, contact, nickname, alias, "
        "or anthropomorphic handle."
    ),
    "organization": (
        "The message is about a company, team, department, school, or other "
        "organization."
    ),
    "none": (
        "There is no explicit subject, or the subject is a project, task, "
        "system, document, or other non-person object."
    ),
}

_QUERY_FOCUS_INSTRUCTION = (
    "判断这条消息问的是主体本人，还是工作对象/一般内部知识。"
    "先判断问题对象是不是人本人，再判断谓词。"
)
_QUERY_FOCUS_CRITERIA: dict[str, str] = {
    "person_self": (
        "Asks about the person themselves: 情况、现状、近况、个人情况、基本信息、"
        "identity, contact details, job title, recent activity, what they said, "
        "or what they did. Use this when the question object is the person, "
        "even if the generic word 情况 is used."
    ),
    "work_object": (
        "Asks about a project, task, system, document, policy, or their "
        "progress, status, stage, or contents. Do not choose this when the "
        "question is about a person's own situation, contact details, or "
        "personal activity."
    ),
    "general_knowledge": (
        "Asks about general company rules, documents, policies, or processes."
    ),
    "other": "None of the above.",
}

_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
)


@dataclass(frozen=True)
class SubjectFocusEvaluation:
    subject_kind: str
    subject_confidence: float
    query_focus: str
    query_confidence: float
    probabilities: dict[str, float]


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
    *,
    include_evidence_plan: bool = False,
    include_subject_focus: bool = False,
) -> dict[str, dict[str, Any]]:
    """The typed questions sent to System One.

    ``include_evidence_plan`` is off by default because the fine-tuned Laya
    checkpoint is positionally bound to the intent question; only the cloud
    backend is asked for the extra source decision. ``include_subject_focus``
    adds the two boundary questions Jev uses to separate person questions from
    knowledge questions before the intent threshold is applied.
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
    if include_subject_focus:
        questions[SUBJECT_KIND_NAME] = {
            "type": "choice",
            "instructions": _SUBJECT_KIND_INSTRUCTION,
            "criteria": dict(_SUBJECT_KIND_CRITERIA),
        }
        questions[QUERY_FOCUS_NAME] = {
            "type": "choice",
            "instructions": _QUERY_FOCUS_INSTRUCTION,
            "criteria": dict(_QUERY_FOCUS_CRITERIA),
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


def parse_subject_focus(raw: Mapping[str, Any]) -> SubjectFocusEvaluation | None:
    """Read the optional person/knowledge boundary answers from a cloud reply.

    A backend that does not implement these questions, or returns an invalid
    option, is treated as "not judged" rather than as a failed request.
    """

    try:
        subject = _parse_choice(
            raw,
            question_name=SUBJECT_KIND_NAME,
            required=False,
        )
        focus = _parse_choice(
            raw,
            question_name=QUERY_FOCUS_NAME,
            required=False,
        )
    except LayaError:
        return None
    if subject is None or focus is None:
        return None
    subject_kind, subject_confidence, subject_probabilities = subject
    query_focus, query_confidence, focus_probabilities = focus
    if subject_kind not in SUBJECT_KIND_VALUES or query_focus not in QUERY_FOCUS_VALUES:
        return None
    probabilities = dict(subject_probabilities)
    probabilities.update(focus_probabilities)
    return SubjectFocusEvaluation(
        subject_kind=subject_kind,
        subject_confidence=subject_confidence,
        query_focus=query_focus,
        query_confidence=query_confidence,
        probabilities=probabilities,
    )


def combine_intent(
    main_label: str,
    main_confidence: float,
    evaluation: SubjectFocusEvaluation | None,
) -> tuple[str, float, str] | None:
    """Turn the two auxiliary answers into a final boundary intent.

    The return value is ``(intent, confidence, reason)``. A caller keeps the
    main intent when this returns ``None``.
    """

    if evaluation is None:
        return None
    subject_kind = evaluation.subject_kind
    subject_confidence = evaluation.subject_confidence
    query_focus = evaluation.query_focus
    query_confidence = evaluation.query_confidence
    if (
        subject_kind == "organization"
        and subject_confidence >= SUBJECT_FOCUS_MIN_CONFIDENCE
    ):
        confidence = max(
            subject_confidence,
            main_confidence if main_label == "knowledge.answer" else 0.0,
        )
        return "knowledge.answer", confidence, "organization_subject"
    if (
        subject_kind == "person"
        and subject_confidence >= SUBJECT_FOCUS_MIN_CONFIDENCE
        and query_confidence >= SUBJECT_FOCUS_MIN_CONFIDENCE
    ):
        confidence = max(subject_confidence, query_confidence)
        if query_focus == "person_self":
            confidence = max(
                confidence,
                main_confidence if main_label == "person.query" else 0.0,
            )
            return "person.query", confidence, "person_self"
        if query_focus == "work_object":
            confidence = max(
                confidence,
                main_confidence if main_label == "knowledge.answer" else 0.0,
            )
            return "knowledge.answer", confidence, "work_object"
    if (
        subject_kind == "none"
        and query_focus == "general_knowledge"
        and query_confidence >= SUBJECT_FOCUS_MIN_CONFIDENCE
    ):
        return "knowledge.answer", query_confidence, "general_knowledge"
    # The main intent and the subject shape agree even when the generic word
    # "情况" leaves query_focus ambiguous. That agreement is enough to rescue
    # a person question from an unnecessary LLM fallback.
    if (
        main_label == "person.query"
        and subject_kind == "person"
        and subject_confidence >= SUBJECT_FOCUS_MIN_CONFIDENCE
    ):
        return (
            "person.query",
            max(main_confidence, subject_confidence),
            "person_subject_agreement",
        )
    if (
        main_label == "knowledge.answer"
        and subject_kind == "organization"
        and subject_confidence >= SUBJECT_FOCUS_MIN_CONFIDENCE
    ):
        return (
            "knowledge.answer",
            max(main_confidence, subject_confidence),
            "organization_subject_agreement",
        )
    return None


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

    parsed = _parse_choice(raw, source=source)
    if parsed is None:  # pragma: no cover - required=True raises instead
        raise LayaError(f"{source} response has no intent answer")
    label, answer_confidence, probabilities = parsed
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
        request_subject_focus: bool = False,
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
        # The cloud backend can also answer the two boundary questions used to
        # separate person questions from internal-knowledge questions.
        self.request_subject_focus = bool(request_subject_focus)
        self.last_call_count = 0
        self.last_fallback_reason: str | None = None
        self.last_answer_confidence: float | None = None
        self.last_margin: float | None = None
        # The versioned model id the backend reported, when it reports one.
        self.last_model: str | None = None
        self.last_subject_kind: str | None = None
        self.last_query_focus: str | None = None
        self.last_aux_confidence: float | None = None
        self.last_combined_intent: str | None = None
        self.last_combined_reason: str | None = None

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
        self.last_subject_kind = None
        self.last_query_focus = None
        self.last_aux_confidence = None
        self.last_combined_intent = None
        self.last_combined_reason = None

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
        else:
            state = {"text": text}
        if self.request_evidence_plan or self.request_subject_focus:
            questions = build_question(
                include_evidence_plan=self.request_evidence_plan,
                include_subject_focus=self.request_subject_focus,
            )
        else:
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
        subject_focus = parse_subject_focus(raw) if self.request_subject_focus else None
        if subject_focus is not None:
            self.last_subject_kind = subject_focus.subject_kind
            self.last_query_focus = subject_focus.query_focus
            self.last_aux_confidence = min(
                subject_focus.subject_confidence,
                subject_focus.query_confidence,
            )
        ordered = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
        second_probability = ordered[1][1] if len(ordered) > 1 else 0.0
        margin = answer_confidence - second_probability
        combined = combine_intent(label, answer_confidence, subject_focus)
        if combined is not None and not (
            label not in {"person.query", "knowledge.answer"}
            and answer_confidence >= threshold
            and margin >= self.min_margin
        ):
            combined_label, combined_confidence, combined_reason = combined
            self.last_combined_intent = combined_label
            self.last_combined_reason = combined_reason
            self.last_answer_confidence = combined_confidence
            self.last_margin = combined_confidence
            return self._combined_evaluation(
                text=text,
                label=combined_label,
                answer_confidence=combined_confidence,
                reason=combined_reason,
                probabilities=probabilities,
                evidence_sources=evidence_sources,
                evidence_reason=evidence_reason,
            )

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

    def _combined_evaluation(
        self,
        *,
        text: str,
        label: str,
        answer_confidence: float,
        reason: str,
        probabilities: dict[str, float],
        evidence_sources: list[str] | None = None,
        evidence_reason: str | None = None,
    ) -> LayaEvaluation:
        """The verdict derived from the two auxiliary boundary questions."""

        combined_probabilities = dict(probabilities)
        # Keep a valid probability distribution for diagnostics: the combined
        # label is the winner and the original main label remains visible as the
        # runner-up when it is a different intent.
        second_label = next(
            (
                name
                for name in sorted(
                    combined_probabilities,
                    key=lambda item: combined_probabilities[item],
                    reverse=True,
                )
                if name != label
            ),
            "",
        )
        combined_probabilities[label] = answer_confidence
        if second_label:
            combined_probabilities[second_label] = max(
                0.0,
                1.0 - answer_confidence,
            )
        margin = answer_confidence - (
            combined_probabilities[second_label] if second_label else 0.0
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
                reason=f"{self.name}: combined {reason}",
                evidence_sources=list(evidence_sources or []),
                evidence_reason=evidence_reason,
            ),
            label=label,
            answer_confidence=answer_confidence,
            margin=margin,
            probabilities=combined_probabilities,
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
            # Jev also answers the two questions that separate person questions
            # from internal-knowledge questions.
            request_subject_focus=True,
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
    question_name: str = "intent",
    required: bool = True,
) -> tuple[str, float, dict[str, float]] | None:
    answers = raw.get("answers")
    if not isinstance(answers, Mapping):
        if required:
            raise LayaError(f"{source} response has no answers object")
        return None
    answer = answers.get(question_name)
    if not isinstance(answer, Mapping):
        if required:
            raise LayaError(f"{source} response has no {question_name} answer")
        return None

    label = answer.get("choice")
    if not isinstance(label, str) or not label.strip():
        if required:
            raise LayaError(f"{source} {question_name} answer has no choice")
        return None

    raw_probabilities = answer.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or not raw_probabilities:
        if required:
            raise LayaError(f"{source} {question_name} answer has no probabilities")
        return None
    probabilities: dict[str, float] = {}
    for name, value in raw_probabilities.items():
        if not isinstance(name, str) or not name:
            if required:
                raise LayaError(
                    f"{source} {question_name} probabilities contain an invalid name"
                )
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if required:
                raise LayaError(
                    f"{source} {question_name} probabilities contain a non-number"
                )
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            if required:
                raise LayaError(
                    f"{source} {question_name} probability is outside [0, 1]"
                )
            return None
        probabilities[name] = number
    if label not in probabilities:
        if required:
            raise LayaError(
                f"{source} {question_name} choice is missing from its probability distribution"
            )
        return None

    # Laya's local server calls it answer_confidence; Jev's cloud API calls it
    # confidence and derives it from the distribution. Never fall back to the
    # top probability: those two numbers are not the same quantity.
    raw_confidence = answer.get("confidence")
    if raw_confidence is None:
        raw_confidence = answer.get("answer_confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        if required:
            raise LayaError(f"{source} {question_name} answer confidence is not a number")
        return None
    answer_confidence = float(raw_confidence)
    if not math.isfinite(answer_confidence) or not 0.0 <= answer_confidence <= 1.0:
        if required:
            raise LayaError(
                f"{source} {question_name} answer confidence is outside [0, 1]"
            )
        return None
    return label.strip(), answer_confidence, probabilities


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
