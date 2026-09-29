"""Resolve PlanStep argument references against prior Observations."""

from __future__ import annotations

from typing import Any

from app.kernel.models import Observation


class ReferenceResolutionError(ValueError):
    def __init__(self, reference: str, message: str) -> None:
        super().__init__(message)
        self.reference = reference


def has_references(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("$")
    if isinstance(value, list):
        return any(has_references(item) for item in value)
    if isinstance(value, dict):
        return any(has_references(item) for item in value.values())
    return False


def resolve_arguments(
    arguments: dict[str, Any],
    observations: list[Observation],
) -> dict[str, Any]:
    by_observation = {item.observation_id: item for item in observations}
    by_step: dict[str, Observation] = {}
    for item in observations:
        if item.status == "succeeded":
            by_step[item.step_id] = item
    return _resolve_value(arguments, by_observation, by_step, referrer="<root>")


def _resolve_value(
    value: Any,
    by_observation: dict[str, Observation],
    by_step: dict[str, Observation],
    *,
    referrer: str,
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_reference(value, by_observation, by_step)
    if isinstance(value, list):
        return [
            _resolve_value(item, by_observation, by_step, referrer=referrer)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _resolve_value(item, by_observation, by_step, referrer=key)
            for key, item in value.items()
        }
    return value


def _resolve_reference(
    reference: str,
    by_observation: dict[str, Observation],
    by_step: dict[str, Observation],
) -> Any:
    parts = reference.split(".")
    if len(parts) < 4:
        raise ReferenceResolutionError(reference, f"invalid reference: {reference}")
    if parts[0] == "$steps":
        observation = by_step.get(parts[1])
    elif parts[0] == "$observations":
        observation = by_observation.get(parts[1])
    else:
        raise ReferenceResolutionError(reference, f"unknown reference namespace: {parts[0]}")
    if observation is None:
        raise ReferenceResolutionError(
            reference, f"referenced observation is not available: {parts[1]}"
        )
    if parts[2] != "output":
        raise ReferenceResolutionError(
            reference, "only output references are supported"
        )
    value: Any = observation.output
    if value is None:
        raise ReferenceResolutionError(reference, "observation has no output")
    for key in parts[3:]:
        if not isinstance(value, dict) or key not in value:
            raise ReferenceResolutionError(
                reference, f"field '{key}' is missing from observation output"
            )
        value = value[key]
    return value
