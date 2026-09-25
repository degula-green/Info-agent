from __future__ import annotations

from app.kernel.models import CapabilityDescriptor, PlanStep, PolicyDecision, TaskEnvelope
from app.kernel.registry import CapabilityRegistry


class FakePolicy:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def evaluate(
        self,
        task: TaskEnvelope,
        step: PlanStep,
        capability: CapabilityDescriptor | None = None,
    ) -> PolicyDecision:
        trusted = self.registry.find(step.capability)
        if trusted is None:
            return PolicyDecision(action="deny", reason=f"unknown capability: {step.capability}")
        descriptor = trusted.descriptor
        if descriptor.side_effect or descriptor.requires_approval:
            return PolicyDecision(action="require_approval", reason="capability has an external side effect")
        return PolicyDecision(action="allow")
