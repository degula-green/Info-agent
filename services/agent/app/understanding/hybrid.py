"""Laya-first intent understanding with an LLM fallback."""

from __future__ import annotations

from app.ingress.vocabulary import references_attachment
from app.kernel.models import TaskEnvelope, TaskUnderstanding
from app.understanding.laya import LayaUnderstandingProvider


class HybridUnderstandingProvider:
    """Uses Laya for clear decisions and the LLM for uncertain cases."""

    name = "hybrid"
    model_backed = True
    # Laya is guaranteed on this path; the LLM call count is added after it runs.
    estimated_model_calls = 1

    def __init__(
        self,
        *,
        laya: LayaUnderstandingProvider,
        fallback,
    ) -> None:
        self.laya = laya
        self.fallback = fallback
        self.model = f"{laya.model}+{getattr(fallback, 'model', 'llm')}"
        self.last_call_count = 0
        self.last_decision_source = ""
        self.last_fallback_reason: str | None = None
        self.last_answer_confidence: float | None = None
        self.last_margin: float | None = None

    def understand(
        self,
        task: TaskEnvelope,
        *,
        min_confidence: float | None = None,
    ) -> TaskUnderstanding:
        self.last_call_count = 0
        self.last_decision_source = ""
        self.last_fallback_reason = None
        self.last_answer_confidence = None
        self.last_margin = None

        # The stock Laya head was trained on short instructions, not on
        # attachment-driven ones ("根据这个附件创建日程" reads like a form
        # request to it). When the user explicitly points at an attachment,
        # let the LLM understanding follow the intent catalog instead.
        text = str(task.input.get("text") or "")
        if task.input.get("attachment_ids") and references_attachment(text):
            result = _call_provider(self.fallback, task, min_confidence=min_confidence)
            self.last_call_count = max(
                int(getattr(self.fallback, "last_call_count", 0)), 0
            )
            self.last_decision_source = "llm"
            self.last_fallback_reason = "attachment_reference"
            return result

        evaluation = None
        try:
            evaluation = self.laya.evaluate(task, min_confidence=min_confidence)
        except Exception as exc:  # noqa: BLE001 - any Laya failure falls back
            self.last_call_count = max(int(getattr(self.laya, "last_call_count", 1)), 1)
            self.last_fallback_reason = _error_reason(exc)

        if evaluation is not None and evaluation.accepted:
            self.last_call_count = max(int(getattr(self.laya, "last_call_count", 0)), 0)
            self.last_decision_source = "laya"
            self.last_answer_confidence = evaluation.answer_confidence
            self.last_margin = evaluation.margin
            return evaluation.understanding

        if evaluation is not None:
            self.last_call_count = max(int(getattr(self.laya, "last_call_count", 0)), 0)
            self.last_fallback_reason = evaluation.fallback_reason or "laya_uncertain"
            self.last_answer_confidence = evaluation.answer_confidence
            self.last_margin = evaluation.margin

        result = _call_provider(self.fallback, task, min_confidence=min_confidence)
        self.last_call_count += max(
            int(getattr(self.fallback, "last_call_count", 0)), 0
        )
        self.last_decision_source = "llm"
        return result


def _error_reason(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "laya_timeout"
    if "unavailable" in name:
        return "laya_unavailable"
    if "lay" in name:
        return "laya_error"
    return "laya_error"


def _call_provider(provider, task: TaskEnvelope, *, min_confidence: float | None):
    if min_confidence is None:
        return provider.understand(task)
    try:
        return provider.understand(task, min_confidence=min_confidence)
    except TypeError:
        return provider.understand(task)


__all__ = ["HybridUnderstandingProvider", "LayaUnderstandingProvider"]
