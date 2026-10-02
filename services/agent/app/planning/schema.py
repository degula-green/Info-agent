"""JSON Schemas for the Planner's strict structured output."""

from __future__ import annotations

import copy
from typing import Any

from app.kernel.models import CapabilityDescriptor, StepOutputRef


def _make_nullable(schema: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)
    if "type" in result:
        value = result["type"]
        if isinstance(value, list):
            if "null" not in value:
                result["type"] = [*value, "null"]
        elif value != "null":
            result["type"] = [value, "null"]
        return result
    if "anyOf" in result or "oneOf" in result or "allOf" in result:
        key = next(item for item in ("anyOf", "oneOf", "allOf") if item in result)
        variants = list(result[key])
        if not any(item.get("type") == "null" for item in variants if isinstance(item, dict)):
            variants.append({"type": "null"})
        result[key] = variants
        return result
    if "$ref" in result:
        return {"anyOf": [{"$ref": result["$ref"]}, {"type": "null"}]}
    return {"anyOf": [result, {"type": "null"}]}


def _strictify(schema: Any) -> Any:
    """Normalize a Pydantic JSON Schema for provider strict mode."""

    if not isinstance(schema, dict):
        return schema
    result = copy.deepcopy(schema)
    result.pop("title", None)
    result.pop("default", None)

    if isinstance(result.get("properties"), dict):
        original_required = set(result.get("required") or [])
        properties: dict[str, Any] = {}
        for name, value in result["properties"].items():
            normalized = _strictify(value)
            if name not in original_required:
                normalized = _make_nullable(normalized)
            properties[name] = normalized
        result["properties"] = properties
        result["required"] = list(properties)
        result["additionalProperties"] = False

    if isinstance(result.get("items"), dict):
        result["items"] = _strictify(result["items"])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [_strictify(item) for item in result[key]]
    for key in ("$defs", "definitions"):
        if isinstance(result.get(key), dict):
            result[key] = {name: _strictify(value) for name, value in result[key].items()}
    return result


def planner_arguments_schema(descriptor: CapabilityDescriptor) -> dict[str, Any]:
    """Build the strict argument schema the Planner may emit for one capability."""

    base = copy.deepcopy(descriptor.planner_input_schema or descriptor.input_schema)
    if not base:
        base = {"type": "object", "properties": {}}
    properties = base.setdefault("properties", {})
    required = set(base.get("required") or [])
    for binding in descriptor.input_bindings:
        properties.pop(binding.runtime_argument, None)
        required.discard(binding.runtime_argument)
        properties[binding.planner_argument] = (
            {
                "type": "array",
                "items": StepOutputRef.model_json_schema(),
                "minItems": 1,
            }
            if binding.aggregate
            else StepOutputRef.model_json_schema()
        )
        if binding.required:
            required.add(binding.planner_argument)
        else:
            required.discard(binding.planner_argument)
    if descriptor.task_text_argument:
        # The model is not asked for it, so it cannot get it wrong: a capability
        # that validates against the user's own words needs a value the model
        # never touched.
        properties.pop(descriptor.task_text_argument, None)
        required.discard(descriptor.task_text_argument)
    base["required"] = sorted(required)
    return _strictify(base)


def _step_variants(capabilities: list[CapabilityDescriptor]) -> list[dict[str, Any]]:
    return [
        {
            "type": "object",
            "properties": {
                "capability": {"type": "string", "enum": [descriptor.name]},
                "arguments": planner_arguments_schema(descriptor),
            },
            "required": ["capability", "arguments"],
            "additionalProperties": False,
        }
        for descriptor in capabilities
    ]


def plan_draft_schema(capabilities: list[CapabilityDescriptor]) -> dict[str, Any]:
    return _strictify(
        {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "minLength": 1},
                "steps": {
                    "type": "array",
                    "items": {"oneOf": _step_variants(capabilities)},
                },
            },
            "required": ["objective", "steps"],
            "additionalProperties": False,
        }
    )


def decision_draft_schema(capabilities: list[CapabilityDescriptor]) -> dict[str, Any]:
    return _strictify(
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "continue",
                        "replan",
                        "request_input",
                        "complete",
                        "fail",
                        "unsupported",
                    ],
                },
                # A model that has just watched answer.compose run sometimes
                # restates the answer here; the field is accepted and ignored,
                # so it has to exist in the schema without inviting content.
                "output": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        {"type": "null"},
                    ]
                },
                # Same instinct, different shape: the model restates the prose
                # answer in the decision. Accepted and ignored.
                "answer": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "required_input": {"type": "array", "items": {"type": "string"}},
                "unsupported_intents": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "requires_user_confirmation": {"type": "boolean"},
                "reason": {"type": ["string", "null"]},
                "steps": {
                    "type": "array",
                    "items": {"oneOf": _step_variants(capabilities)},
                },
            },
            "required": [
                "action",
                "output",
                "required_input",
                "unsupported_intents",
                "warnings",
                "requires_user_confirmation",
                "reason",
                "steps",
            ],
            "additionalProperties": False,
        }
    )


__all__ = [
    "decision_draft_schema",
    "plan_draft_schema",
    "planner_arguments_schema",
]
