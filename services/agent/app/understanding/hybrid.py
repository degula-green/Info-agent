"""Primary intent classifier with an LLM fallback.

The primary is the fast, cheap, calibrated path (Laya locally, or Jev in the
cloud). Anything it cannot decide confidently — low confidence, thin margin,
competing labels, or an error — is handed to the LLM, which is slower but has
the widest coverage.
"""

from __future__ import annotations

from app.kernel.models import TaskEnvelope, TaskUnderstanding
from app.understanding.laya import (
    JevUnderstandingProvider,
    LayaUnderstandingProvider,
    SystemOneUnderstandingProvider,
)


class HybridUnderstandingProvider:
    """Uses the primary classifier for clear decisions and the LLM otherwise."""

    name = "hybrid"
    model_backed = True
    # The primary call is guaranteed on this path; the LLM call count is added
    # after it runs.
    estimated_model_calls = 1

    def __init__(
        self,
        *,
        primary: SystemOneUnderstandingProvider | None = None,
        fallback,
        laya: SystemOneUnderstandingProvider | None = None,
    ) -> None:
        # ``laya=`` is the historical keyword; ``primary=`` carries the general
        # case now that Jev can take the fast path too.
        resolved = primary if primary is not None else laya
        if resolved is None:
            raise TypeError(
                "HybridUnderstandingProvider requires 'primary' (or legacy 'laya')"
            )
        self.primary = resolved
        self.laya = resolved  # backward-compatible attribute
        self.fallback = fallback
        self.model = f"{resolved.model}+{getattr(fallback, 'model', 'llm')}"
        self.last_call_count = 0
        self.last_decision_source = ""
        self.last_fallback_reason: str | None = None
        self.last_answer_confidence: float | None = None
        self.last_margin: float | None = None
        # Generic aliases: the runtime records these so observation is not tied
        # to whichever model happens to be primary.
        self.last_primary_confidence: float | None = None
        self.last_primary_margin: float | None = None
        self.intents = getattr(resolved, "intents", None)
        self.available_intents = self.intents

    @property
    def primary_name(self) -> str:
        return str(getattr(self.primary, "name", "primary"))

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
        self.last_primary_confidence = None
        self.last_primary_margin = None

        evaluation = None
        try:
            evaluation = self.primary.evaluate(task, min_confidence=min_confidence)
        except Exception as exc:  # noqa: BLE001 - any primary failure falls back
            self.last_call_count = max(
                int(getattr(self.primary, "last_call_count", 1)), 1
            )
            self.last_fallback_reason = _error_reason(exc, self.primary_name)

        if evaluation is not None and evaluation.accepted:
            self.last_call_count = max(
                int(getattr(self.primary, "last_call_count", 0)), 0
            )
            self.last_decision_source = self.primary_name
            self._record_confidence(evaluation.answer_confidence, evaluation.margin)
            return evaluation.understanding

        if evaluation is not None:
            self.last_call_count = max(
                int(getattr(self.primary, "last_call_count", 0)), 0
            )
            self.last_fallback_reason = (
                evaluation.fallback_reason or f"{self.primary_name}_uncertain"
            )
            self._record_confidence(evaluation.answer_confidence, evaluation.margin)

        result = _call_provider(self.fallback, task, min_confidence=min_confidence)
        self.last_call_count += max(
            int(getattr(self.fallback, "last_call_count", 0)), 0
        )
        self.last_decision_source = "llm"
        return result

    def _record_confidence(self, confidence: float, margin: float) -> None:
        self.last_answer_confidence = confidence
        self.last_margin = margin
        self.last_primary_confidence = confidence
        self.last_primary_margin = margin


def _error_reason(exc: Exception, name: str) -> str:
    kind = type(exc).__name__.lower()
    if "timeout" in kind:
        return f"{name}_timeout"
    if "unavailable" in kind:
        return f"{name}_unavailable"
    return f"{name}_error"


def _call_provider(provider, task: TaskEnvelope, *, min_confidence: float | None):
    if min_confidence is None:
        return provider.understand(task)
    try:
        return provider.understand(task, min_confidence=min_confidence)
    except TypeError:
        return provider.understand(task)


__all__ = [
    "HybridUnderstandingProvider",
    "JevUnderstandingProvider",
    "LayaUnderstandingProvider",
]
