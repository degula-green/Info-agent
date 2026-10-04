"""Dependency normalization and readiness rules for Plan DAGs."""

from __future__ import annotations

from typing import Any

from app.kernel.errors import ContractValidationError
from app.kernel.models import Plan, PlanStep


def referenced_step_ids(value: Any) -> set[str]:
    """Return every ``$steps.<id>.output...`` dependency in one argument tree."""

    found: set[str] = set()
    if isinstance(value, str):
        if value.startswith("$steps."):
            parts = value.split(".")
            if len(parts) >= 4 and parts[2] == "output" and parts[1]:
                found.add(parts[1])
        return found
    if isinstance(value, list):
        for item in value:
            found.update(referenced_step_ids(item))
        return found
    if isinstance(value, dict):
        for item in value.values():
            found.update(referenced_step_ids(item))
    return found


def normalize_plan_dependencies(plan: Plan) -> Plan:
    """Materialize legacy sequential dependencies and merge referenced steps."""

    normalized = plan.model_copy(deep=True)
    ordered = sorted(normalized.steps, key=lambda item: item.order)
    by_id = {step.step_id: step for step in ordered}
    previous: str | None = None
    original: dict[str, list[str] | None] = {}

    for step in ordered:
        original[step.step_id] = (
            None if step.depends_on is None else list(step.depends_on)
        )

    for step in ordered:
        if step.depends_on is None:
            dependencies = {previous} if previous else set()
        else:
            dependencies = set(step.depends_on)
        dependencies.update(referenced_step_ids(step.arguments))
        dependencies.discard(step.step_id)

        missing = sorted(item for item in dependencies if item not in by_id)
        if missing:
            raise ContractValidationError(
                "plan dependency validation failed",
                [
                    f"{step.step_id} depends on missing step {item}"
                    for item in missing
                ],
            )
        later = sorted(
            item for item in dependencies if by_id[item].order >= step.order
        )
        if later:
            raise ContractValidationError(
                "plan dependency validation failed",
                [
                    f"{step.step_id} depends on non-earlier step {item}"
                    for item in later
                ],
            )
        step.depends_on = sorted(
            dependencies,
            key=lambda item: by_id[item].order,
        )
        previous = step.step_id

    validate_acyclic(normalized.steps)
    return normalized


def validate_acyclic(steps: list[PlanStep]) -> None:
    """Reject cycles even though the normalizer also enforces earlier edges."""

    graph = {step.step_id: list(step.depends_on or []) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle: list[str] = []

    def visit(node: str) -> bool:
        if node in visiting:
            cycle.append(node)
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, []):
            if visit(dependency):
                cycle.append(node)
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for step_id in graph:
        if visit(step_id):
            ordered = " -> ".join(reversed(cycle))
            raise ContractValidationError(
                "plan dependency cycle",
                [ordered],
            )


def ready_steps(steps: list[PlanStep]) -> list[PlanStep]:
    """Pending/ready Steps whose dependencies have all succeeded."""

    by_id = {step.step_id: step for step in steps}
    ready: list[PlanStep] = []
    for step in sorted(steps, key=lambda item: item.order):
        if step.status not in {"pending", "ready"}:
            continue
        if all(
            by_id.get(dependency) is not None
            and by_id[dependency].status == "succeeded"
            for dependency in (step.depends_on or [])
        ):
            ready.append(step)
    return ready


__all__ = [
    "normalize_plan_dependencies",
    "ready_steps",
    "referenced_step_ids",
    "validate_acyclic",
]
