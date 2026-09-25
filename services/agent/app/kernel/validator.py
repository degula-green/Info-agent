from __future__ import annotations

from app.kernel.errors import ContractValidationError
from app.kernel.models import Plan, PlanStep, TaskEnvelope
from app.kernel.registry import CapabilityRegistry


class PlanValidator:
    def __init__(self, registry: CapabilityRegistry, max_steps: int = 20) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.registry = registry
        self.max_steps = max_steps

    def validate(self, plan: Plan, task: TaskEnvelope | None = None) -> None:
        errors: list[str] = []
        if task is not None and plan.task_id != task.task_id:
            errors.append("plan task_id does not match task")
        if len(plan.steps) > self.max_steps:
            errors.append(f"plan exceeds maximum step count: {self.max_steps}")

        step_ids = [step.step_id for step in plan.steps]
        duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
        errors.extend(f"duplicate step_id: {step_id}" for step_id in duplicates)

        expected_orders = list(range(1, len(plan.steps) + 1))
        actual_orders = [step.order for step in plan.steps]
        if actual_orders != expected_orders:
            errors.append("steps must be ordered sequentially starting at 1")

        for step in plan.steps:
            errors.extend(self._step_errors(plan, step))

        if errors:
            raise ContractValidationError("plan validation failed", errors)

    def validate_step(
        self,
        plan: Plan,
        step: PlanStep,
        task: TaskEnvelope | None = None,
    ) -> None:
        errors = self._step_errors(plan, step)
        if task is not None and plan.task_id != task.task_id:
            errors.append("plan task_id does not match task")
        if errors:
            raise ContractValidationError("step validation failed", errors)

    def _step_errors(self, plan: Plan, step: PlanStep) -> list[str]:
        errors: list[str] = []
        if step.plan_id != plan.plan_id:
            errors.append(f"step {step.step_id} does not belong to plan")
        capability = self.registry.find(step.capability)
        if capability is None:
            errors.append(f"unknown capability: {step.capability}")
            return errors
        try:
            capability.validate(step.arguments)
        except Exception as exc:
            errors.append(f"invalid arguments for {step.capability}: {exc}")
        return errors
