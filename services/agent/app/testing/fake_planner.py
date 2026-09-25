from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from app.kernel.models import CapabilityDescriptor, Observation, Plan, PlanStep, TaskEnvelope, TaskUnderstanding


class FakeTaskUnderstandingProvider:
    def __init__(self, understanding: TaskUnderstanding | None = None) -> None:
        self.understanding = understanding

    def understand(self, task: TaskEnvelope) -> TaskUnderstanding:
        return self.understanding or TaskUnderstanding(goal=str(task.input.get("text", "")))


class FakePlanner:
    def __init__(self, steps: Sequence[dict[str, Any]] | None = None) -> None:
        self.steps = list(steps or [{"capability": "fake.read", "arguments": {"value": "ok"}}])

    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
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
