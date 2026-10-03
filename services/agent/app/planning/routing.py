"""Send each Task to the planner that can actually serve its intent.

The information-collection entry is a fixed pipeline -- collected text in, at
most one to-do out -- and must not become a model decision, because a model
that "improves" it can only invent work the user never asked for. Chat turns
are routed by their v6 intent, so only the ones that genuinely need
composition pay for a model call; a keyword search is not a reason to run the
LLM planner over a to-do.
"""

from __future__ import annotations

import inspect

from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    TaskEnvelope,
    TaskUnderstanding,
)
from app.planning.conversation_memory import (
    build_conversation_memory_plan,
    decide_conversation_memory_plan,
)

WEB_RESEARCH_INTENT = "web.research"
KNOWLEDGE_ANSWER_INTENT = "knowledge.answer"
FORM_COMPLETE_INTENT = "form.complete"
# Intents whose plan has to be composed from several capabilities. The
# container adds ``knowledge.answer`` when the internal knowledge tools are
# registered; otherwise the deterministic planner is the only one that can
# serve it (attachments only).
DEFAULT_LLM_INTENTS = frozenset({WEB_RESEARCH_INTENT})
# Entries whose text arrives from collection rather than from a person typing.
DEFAULT_DETERMINISTIC_SOURCE_TYPES = frozenset({"knowledge_event"})


class RoutingPlanner:
    """Delegates to one of two planners; it never plans anything itself."""

    name = "routing"

    def __init__(
        self,
        *,
        deterministic,
        llm,
        llm_intents: frozenset[str] = DEFAULT_LLM_INTENTS,
        deterministic_source_types: frozenset[str] = DEFAULT_DETERMINISTIC_SOURCE_TYPES,
    ) -> None:
        self.deterministic = deterministic
        self.llm = llm
        self.llm_intents = frozenset(llm_intents)
        self.deterministic_source_types = frozenset(deterministic_source_types)

    def set_validators(self, validators: dict) -> None:
        """The LLM planner re-asks on schema misses; it needs the validators."""

        attach = getattr(self.llm, "set_validators", None)
        if attach is not None:
            attach(validators)

    def route(
        self, task: TaskEnvelope, understanding: TaskUnderstanding | None
    ) -> object:
        """The planner for this Task; collection never reaches the LLM planner."""

        if str(task.source_type or "") in self.deterministic_source_types:
            return self.deterministic
        if understanding is None:
            return self.deterministic
        intents = {item.name for item in understanding.intent_candidates}
        if intents & self.llm_intents:
            return self.llm
        return self.deterministic

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
        *,
        conversation_context=None,
    ) -> Plan:
        memory_plan = build_conversation_memory_plan(task, capabilities)
        if memory_plan is not None:
            self._last_call_count = 0
            return memory_plan
        planner = self.route(task, understanding)
        return _call_planner(
            planner.create_plan,
            task,
            capabilities,
            observations,
            constraints,
            understanding,
            conversation_context=conversation_context,
        )

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
        *,
        conversation_context=None,
    ) -> PlannerDecision:
        memory_decision = decide_conversation_memory_plan(
            current_plan,
            observations,
        )
        if memory_decision is not None:
            self._last_call_count = 0
            return memory_decision
        planner = self.route(task, understanding)
        return _call_planner(
            planner.decide_after_observation,
            task,
            current_plan,
            observations,
            constraints,
            understanding,
            conversation_context=conversation_context,
        )


def _call_planner(call, *args, conversation_context=None):
    signature = inspect.signature(call)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs or "conversation_context" in signature.parameters:
        return call(*args, conversation_context=conversation_context)
    return call(*args)
