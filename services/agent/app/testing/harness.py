from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.models import ApprovalRequest, Observation, Plan, PolicyDecision, TaskEnvelope, TaskUnderstanding
from app.kernel.registry import CapabilityRegistry
from app.kernel.validator import PlanValidator
from app.kernel.protocols import Planner, TaskUnderstandingProvider
from app.testing.in_memory_store import InMemoryTaskStore
from app.testing.fake_policy import FakePolicy


@dataclass
class HarnessResult:
    task: TaskEnvelope
    plan: Plan
    understanding: TaskUnderstanding | None = None
    observations: list[Observation] = field(default_factory=list)
    decisions: list[PolicyDecision] = field(default_factory=list)
    executed_steps: list[str] = field(default_factory=list)
    approval_request: ApprovalRequest | None = None


class FakeHarness:
    def __init__(
        self,
        registry: CapabilityRegistry,
        planner: Planner,
        policy: FakePolicy,
        store: InMemoryTaskStore,
        understanding_provider: TaskUnderstandingProvider | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.policy = policy
        self.store = store
        self.understanding_provider = understanding_provider
        self.validator = validator or PlanValidator(registry)

    def run(self, task: TaskEnvelope) -> HarnessResult:
        self.store.save_task(task)
        understanding = self.understanding_provider.understand(task) if self.understanding_provider is not None else None
        plan = self.planner.create_plan(task, self.registry.list_descriptors(), self.store.list_for_task(task.task_id))
        self.validator.validate(plan, task)
        self.store.save_plan(plan)
        result = HarnessResult(task=task, plan=plan, understanding=understanding)

        for step in plan.steps:
            capability = self.registry.get(step.capability)
            decision = self.policy.evaluate(task, step, capability.descriptor)
            result.decisions.append(decision)
            if decision.action == "deny":
                step.status = "failed"
                plan.status = "failed"
                self.store.save_plan(plan)
                return result
            if decision.action == "require_approval":
                step.status = "waiting_approval"
                plan.status = "waiting_approval"
                result.approval_request = ApprovalRequest(
                    task_id=task.task_id,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    capability=step.capability,
                    arguments=step.arguments,
                    reason=decision.reason,
                )
                self.store.save_plan(plan)
                return result

            step.status = "running"
            try:
                arguments = capability.validate(step.arguments)
                output = capability.execute(arguments)
            except Exception as exc:
                step.status = "failed"
                plan.status = "failed"
                observation = self._observation(task, plan, step, "failed", error={"type": type(exc).__name__, "message": str(exc)})
                result.observations.append(observation)
                self.store.save(observation)
                self.store.save_plan(plan)
                return result

            step.status = "succeeded"
            observation = self._observation(task, plan, step, "succeeded", output=output)
            result.observations.append(observation)
            result.executed_steps.append(step.step_id)
            self.store.save(observation)

        plan.status = "succeeded"
        self.store.save_plan(plan)
        return result

    @staticmethod
    def _observation(
        task: TaskEnvelope,
        plan: Plan,
        step: Any,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> Observation:
        return Observation(
            observation_id=str(uuid4()),
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            capability=step.capability,
            status=status,
            output=output,
            error=error,
            created_at=datetime.now(timezone.utc),
        )
