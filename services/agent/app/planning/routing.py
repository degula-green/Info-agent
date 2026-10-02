"""Send each Task to the planner that can actually serve its intent.

The information-collection entry is a fixed pipeline -- collected text in, at
most one to-do out -- and must not become a model decision, because a model
that "improves" it can only invent work the user never asked for. Chat turns
are routed by their v6 intent, so only the ones that genuinely need
composition pay for a model call; a keyword search is not a reason to run the
LLM planner over a to-do.
"""

from __future__ import annotations

from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    TaskEnvelope,
    TaskUnderstanding,
)

WEB_RESEARCH_INTENT = "web.research"
DEFAULT_WEB_INTENTS = frozenset({WEB_RESEARCH_INTENT})
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
        web_intents: frozenset[str] = DEFAULT_WEB_INTENTS,
        deterministic_source_types: frozenset[str] = DEFAULT_DETERMINISTIC_SOURCE_TYPES,
    ) -> None:
        self.deterministic = deterministic
        self.llm = llm
        self.web_intents = frozenset(web_intents)
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
        if intents & self.web_intents:
            return self.llm
        return self.deterministic

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        planner = self.route(task, understanding)
        return planner.create_plan(
            task, capabilities, observations, constraints, understanding
        )

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        planner = self.route(task, understanding)
        return planner.decide_after_observation(
            task, current_plan, observations, constraints, understanding
        )
