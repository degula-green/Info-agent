"""Understanding provider implementations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import ValidationError

from app.infrastructure.llm.client import OpenAIChatClient, LLMError, parse_json_object
from app.ingress.vocabulary import SCHEDULE_KEYWORDS, TIME_PHRASE_PATTERN
from app.kernel.models import TaskEnvelope, TaskUnderstanding
from app.understanding.prompt import (
    DEFAULT_MIN_CONFIDENCE,
    build_repair_messages,
    build_understanding_messages,
)
from app.understanding.schema import (
    INTENT_CATALOG,
    IntentDefinition,
    TaskUnderstandingDraft,
    intent_catalog_text,
)


class UnderstandingProviderError(RuntimeError):
    classification = "permanent_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.classification = "retryable_error" if retryable else "permanent_error"


class LLMClient(Protocol):
    model: str
    last_call_count: int

    def complete(self, messages: list[dict[str, str]]) -> str:
        ...


class FakeUnderstandingProvider:
    name = "fake"
    model = "fake"
    model_backed = False
    estimated_model_calls = 0

    def __init__(self, understanding: TaskUnderstanding | None = None) -> None:
        self.understanding = understanding
        self.last_call_count = 0

    def understand(self, task: TaskEnvelope) -> TaskUnderstanding:
        self.last_call_count = 0
        if self.understanding is not None:
            return self.understanding.model_copy(deep=True)
        text = str(task.input.get("text") or "").strip()
        return TaskUnderstanding(
            is_task=bool(text),
            goal=text,
            task_kind="action" if text else None,
            confidence=0.5 if text else 0.0,
            reason="scripted fake provider",
        )


class RuleBasedUnderstandingProvider:
    name = "rules"
    model = "rules"
    model_backed = False
    estimated_model_calls = 0

    def __init__(
        self,
        *,
        available_intents: Iterable[IntentDefinition] | None = None,
    ) -> None:
        self.last_call_count = 0
        self.intents = (
            INTENT_CATALOG
            if available_intents is None
            else tuple(available_intents)
        )
        self.available_names = frozenset(item.name for item in self.intents)

    def understand(self, task: TaskEnvelope) -> TaskUnderstanding:
        self.last_call_count = 0
        text = str(task.input.get("text") or "").strip()
        if not text:
            return TaskUnderstanding(
                is_task=False,
                goal="",
                confidence=1.0,
                reason="empty input",
            )
        has_schedule_word = any(word in text for word in SCHEDULE_KEYWORDS)
        has_time = TIME_PHRASE_PATTERN.search(text) is not None
        if has_schedule_word and has_time and "todo.create" in self.available_names:
            return TaskUnderstanding(
                is_task=True,
                goal=text,
                task_kind="action",
                intent_candidates=[
                    {
                        "name": "todo.create",
                        "confidence": 0.72,
                        "evidence": text,
                    }
                ],
                confidence=0.72,
                reason="rule matched a scheduling keyword and a time phrase",
            )
        return TaskUnderstanding(
            is_task=False,
            goal=text,
            task_kind=None,
            confidence=0.6,
            reason="no conservative scheduling pattern matched",
        )


class OpenAICompatibleUnderstandingProvider:
    name = "llm"
    model_backed = True
    estimated_model_calls = 2

    def __init__(
        self,
        client: LLMClient,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        available_intents: Iterable[IntentDefinition] | None = None,
    ) -> None:
        self.client = client
        self.model = str(getattr(client, "model", ""))
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self.last_call_count = 0
        self._last_error = ""
        self.intents = (
            INTENT_CATALOG
            if available_intents is None
            else tuple(available_intents)
        )
        self.available_names = frozenset(item.name for item in self.intents)
        self.catalog_text = intent_catalog_text(self.intents)

    def understand(
        self,
        task: TaskEnvelope,
        *,
        min_confidence: float | None = None,
        conversation_context: Any | None = None,
    ) -> TaskUnderstanding:
        self.last_call_count = 0
        threshold = (
            self.min_confidence
            if min_confidence is None
            else max(0.0, min(1.0, float(min_confidence)))
        )
        messages = build_understanding_messages(
            task,
            threshold,
            catalog_text=self.catalog_text,
            conversation_context=conversation_context,
        )
        try:
            raw = self.client.complete(messages)
        except LLMError as exc:
            raise UnderstandingProviderError(
                str(exc), retryable=exc.classification == "retryable_error"
            ) from exc
        self.last_call_count += max(int(getattr(self.client, "last_call_count", 1)), 1)
        draft = self._parse(raw, threshold)
        if draft is not None:
            return draft.to_understanding()

        try:
            repaired = self.client.complete(
                build_repair_messages(
                    messages,
                    raw,
                    self._last_error,
                    min_confidence=threshold,
                )
            )
        except LLMError as exc:
            raise UnderstandingProviderError(
                str(exc), retryable=exc.classification == "retryable_error"
            ) from exc
        self.last_call_count += max(int(getattr(self.client, "last_call_count", 1)), 1)
        draft = self._parse(repaired, threshold)
        if draft is None:
            raise UnderstandingProviderError(
                f"understanding output failed after repair: {self._last_error}"
            )
        return draft.to_understanding()

    def _parse(
        self, raw: str, threshold: float | None = None
    ) -> TaskUnderstandingDraft | None:
        try:
            draft = TaskUnderstandingDraft.model_validate(parse_json_object(raw))
        except (ValueError, ValidationError) as exc:
            self._last_error = str(exc)
            return None
        # The model is asked to drop weak candidates; this makes the contract
        # hold even when it does not, so the planner never sees a guess that
        # scored below the configured threshold.
        resolved = self.min_confidence if threshold is None else threshold
        draft.intent_candidates = [
            item
            for item in draft.intent_candidates
            if item.confidence >= resolved
            # A candidate the deployment cannot execute is dropped rather than
            # handed to a planner that has no step for it. The model is already
            # shown only the available catalog; this keeps a stale or creative
            # answer from widening the plan anyway.
            and item.name in self.available_names
        ]
        return draft
