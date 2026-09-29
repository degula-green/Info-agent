from __future__ import annotations

from pydantic import BaseModel, Field

from app.kernel.models import CapabilityDescriptor


class FakeValueInput(BaseModel):
    value: str = Field(min_length=1)


class FakeReadCapability:
    descriptor = CapabilityDescriptor(
        name="fake.read",
        description="Read a value without an external side effect.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def validate(self, arguments: dict) -> FakeValueInput:
        return FakeValueInput.model_validate(arguments)

    def execute(self, arguments: FakeValueInput) -> dict:
        return {"operation": "read", "value": arguments.value}


class FakeWriteCapability:
    descriptor = CapabilityDescriptor(
        name="fake.write",
        description="Write a value and model an external side effect.",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.execute_count = 0

    def validate(self, arguments: dict) -> FakeValueInput:
        return FakeValueInput.model_validate(arguments)

    def execute(self, arguments: FakeValueInput) -> dict:
        self.execute_count += 1
        return {"operation": "write", "value": arguments.value}


class FakeAskInputCapability:
    """Read-only capability that pauses the Task until the user supplies data."""

    descriptor = CapabilityDescriptor(
        name="fake.ask",
        description="Request missing information from the user.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self, missing: list[str] | None = None) -> None:
        self.missing = missing or ["required_field"]

    def validate(self, arguments: dict) -> FakeValueInput:
        return FakeValueInput.model_validate({"value": arguments.get("value") or "placeholder"})

    def execute(self, arguments: FakeValueInput) -> dict:
        return {
            "operation": "ask",
            "requires_user_input": True,
            "missing_information": list(self.missing),
        }
