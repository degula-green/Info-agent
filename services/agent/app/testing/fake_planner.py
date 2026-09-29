from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
    TaskUnderstanding,
)


class FakeTaskUnderstandingProvider:
    def __init__(self, understanding: TaskUnderstanding | None = None) -> None:
        self.understanding = understanding
        self.last_call_count = 0

    def understand(self, task: TaskEnvelope) -> TaskUnderstanding:
        self.last_call_count = 0
        return self.understanding or TaskUnderstanding(
            is_task=True,
            goal=str(task.input.get("text", "")),
        )


class FakePlanner:
    def __init__(self, steps: Sequence[dict[str, Any]] | None = None) -> None:
        self.steps = list(steps or [{"capability": "fake.read", "arguments": {"value": "ok"}}])

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        plan_id = str(uuid4())
        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective=str(task.input.get("text", "fake objective")),
            steps=[
                PlanStep(
                    step_id=f"step-{index}",
                    plan_id=plan_id,
                    order=index,
                    capability=step["capability"],
                    arguments=dict(step.get("arguments", {})),
                )
                for index, step in enumerate(self.steps, start=1)
            ],
        )

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        latest = observations[-1] if observations else None
        if latest is not None and latest.output and latest.output.get("requires_user_input"):
            return PlannerDecision(
                action="request_input",
                required_input=list(latest.output.get("missing_information") or []),
                reason="capability requires user input",
            )
        if observations and observations[-1].status != "succeeded":
            return PlannerDecision(
                action="fail",
                reason="fake planner does not recover from failed observations",
            )
        if any(step.status in {"pending", "ready", "running"} for step in current_plan.steps):
            return PlannerDecision(action="continue")
        return PlannerDecision(action="complete")


class InputDrivenFakePlanner:
    """Step-1 planner: derives sequential steps from the Task input.

    ``input["steps"]`` may carry an explicit list of ``{"capability", "arguments"}``
    entries. Without it the Task defaults to a single ``fake.read`` step, which
    keeps the kernel verifiable before real planning exists.
    """

    def __init__(self, default_capability: str = "fake.read") -> None:
        self.default_capability = default_capability

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        raw_steps = task.input.get("steps")
        steps: list[dict[str, Any]] = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if isinstance(item, dict) and item.get("capability"):
                    steps.append(
                        {
                            "capability": str(item["capability"]),
                            "arguments": dict(item.get("arguments") or {}),
                        }
                    )
        if not steps:
            steps = [
                {
                    "capability": self.default_capability,
                    "arguments": {"value": str(task.input.get("text") or "ok")},
                }
            ]

        plan_id = str(uuid4())
        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective=str(task.input.get("objective") or task.input.get("text") or "fake objective"),
            steps=[
                PlanStep(
                    step_id=f"{plan_id}-step-{index}",
                    plan_id=plan_id,
                    order=index,
                    capability=step["capability"],
                    arguments=step["arguments"],
                )
                for index, step in enumerate(steps, start=1)
            ],
        )

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        latest = observations[-1] if observations else None
        if latest is not None and latest.output and latest.output.get("requires_user_input"):
            return PlannerDecision(
                action="request_input",
                required_input=list(latest.output.get("missing_information") or []),
                reason="capability requires user input",
            )
        if observations and observations[-1].status != "succeeded":
            return PlannerDecision(
                action="fail",
                reason="input-driven planner does not recover from failed observations",
            )
        if any(step.status in {"pending", "ready", "running"} for step in current_plan.steps):
            return PlannerDecision(action="continue")
        return PlannerDecision(action="complete")


class FakeReplanner(FakePlanner):
    """A scripted planner for exercising one or more replanning cycles."""

    def __init__(
        self,
        *,
        initial_steps: Sequence[dict[str, Any]],
        replan_steps: Sequence[dict[str, Any]],
        max_replans: int = 1,
        trigger_classifications: Sequence[str] = ("retryable_error",),
    ) -> None:
        super().__init__(initial_steps)
        self.initial_steps = list(initial_steps)
        self.replan_steps = list(replan_steps)
        self.max_replans = max_replans
        self.trigger_classifications = set(trigger_classifications)
        self.replan_count = 0

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints | None = None,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        self.steps = list(self.initial_steps)
        return super().create_plan(task, capabilities, observations, constraints, understanding)

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        latest = observations[-1] if observations else None
        if latest is not None and latest.output and latest.output.get("requires_user_input"):
            return PlannerDecision(
                action="request_input",
                required_input=list(latest.output.get("missing_information") or []),
                reason="capability requires user input",
            )
        if latest is not None and latest.status != "succeeded":
            classification = str((latest.error or {}).get("classification") or "")
            if (
                self.replan_count < min(self.max_replans, constraints.max_replans)
                and classification in self.trigger_classifications
            ):
                self.replan_count += 1
                self.steps = list(self.replan_steps)
                plan_id = str(uuid4())
                plan = Plan(
                    plan_id=plan_id,
                    task_id=task.task_id,
                    parent_plan_id=current_plan.plan_id,
                    triggered_by_observation_id=latest.observation_id,
                    objective=str(task.input.get("text", "fake objective")),
                    steps=[
                        PlanStep(
                            step_id=f"{plan_id}-step-{index}",
                            plan_id=plan_id,
                            order=index,
                            capability=item["capability"],
                            arguments=dict(item.get("arguments", {})),
                        )
                        for index, item in enumerate(self.replan_steps, start=1)
                    ],
                )
                plan.replan_reason = latest.error.get("message") if latest.error else None
                return PlannerDecision(
                    action="replan",
                    plan=plan,
                    reason="scripted replan after failed observation",
                )
            return PlannerDecision(
                action="fail",
                reason="scripted replanner exhausted or classification not configured",
            )
        if latest is not None and latest.status == "unknown":
            return PlannerDecision(
                action="fail",
                reason="unknown external result requires manual handling",
            )
        if any(step.status in {"pending", "ready", "running"} for step in current_plan.steps):
            return PlannerDecision(action="continue")
        return PlannerDecision(action="complete")
