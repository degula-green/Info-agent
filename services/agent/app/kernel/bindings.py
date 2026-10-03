"""Resolve planner-facing StepOutputRef arguments into runtime references."""

from __future__ import annotations

from app.kernel.errors import ContractValidationError
from app.kernel.models import CapabilityDescriptor, Plan, StepOutputRef


def bind_plan_references(
    plan: Plan,
    capabilities: list[CapabilityDescriptor],
) -> Plan:
    """Returns a copy of ``plan`` with reference arguments normalized.

    The Planner emits values such as::

        {"document_ref": {"step": 1, "output": "content"}}

    The runtime receives the existing canonical form::

        {"document": "$steps.<step_id>.output.content"}
    """

    descriptors = {item.name: item for item in capabilities}
    bound = plan.model_copy(deep=True)
    by_order = {step.order: step for step in bound.steps}
    errors: list[str] = []

    for step in sorted(bound.steps, key=lambda item: item.order):
        descriptor = descriptors.get(step.capability)
        if descriptor is None:
            errors.append(f"unknown capability: {step.capability}")
            continue
        for binding in descriptor.input_bindings:
            if (
                binding.planner_argument not in step.arguments
                or step.arguments.get(binding.planner_argument) is None
            ):
                continue
            raw_reference = step.arguments.get(binding.planner_argument)
            raw_references = raw_reference if binding.aggregate else [raw_reference]
            if binding.aggregate and not isinstance(raw_references, list):
                errors.append(
                    f"{step.step_id}.{binding.planner_argument} must be an array of step references"
                )
                continue
            if binding.aggregate and not raw_references:
                errors.append(
                    f"{step.step_id}.{binding.planner_argument} must contain at least one step reference"
                )
                continue

            canonical_references: list[str] = []
            for item in raw_references:
                try:
                    reference = StepOutputRef.model_validate(item)
                except Exception as exc:  # noqa: BLE001 - report a compact contract error
                    errors.append(
                        f"{step.step_id}.{binding.planner_argument} is not a valid step reference: {exc}"
                    )
                    continue

                target = by_order.get(reference.step)
                if target is None:
                    errors.append(
                        f"{step.step_id}.{binding.planner_argument} references missing step {reference.step}"
                    )
                    continue
                if target.order >= step.order:
                    errors.append(
                        f"{step.step_id}.{binding.planner_argument} must reference an earlier step"
                    )
                    continue
                if target.capability != binding.source_capability:
                    errors.append(
                        f"{step.step_id}.{binding.planner_argument} references "
                        f"{target.capability}, expected {binding.source_capability}"
                    )
                    continue
                if reference.output != binding.source_output:
                    errors.append(
                        f"{step.step_id}.{binding.planner_argument} references output "
                        f"{reference.output}, expected {binding.source_output}"
                    )
                    continue

                target_descriptor = descriptors.get(target.capability)
                if target_descriptor is not None:
                    output_properties = target_descriptor.output_schema.get("properties", {})
                    if binding.source_output not in output_properties:
                        errors.append(
                            f"{target.capability} does not declare output {binding.source_output}"
                        )
                        continue
                canonical_references.append(
                    f"$steps.{target.step_id}.output.{binding.source_output}"
                )

            if not canonical_references:
                continue
            if binding.runtime_argument in step.arguments:
                errors.append(
                    f"{step.step_id} contains both {binding.planner_argument} "
                    f"and {binding.runtime_argument}"
                )
                continue
            step.arguments[binding.runtime_argument] = (
                {"$concat": canonical_references}
                if binding.aggregate
                else canonical_references[0]
            )
            del step.arguments[binding.planner_argument]

    if errors:
        raise ContractValidationError("plan reference binding failed", errors)
    return bound


__all__ = ["bind_plan_references"]
