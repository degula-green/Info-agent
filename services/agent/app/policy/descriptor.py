"""Descriptor-driven policy: the registry is the only trusted source."""

from __future__ import annotations

from app.kernel.models import CapabilityDescriptor, PlanStep, PolicyDecision, TaskEnvelope
from app.kernel.registry import CapabilityRegistry


class DescriptorPolicy:
    """Reads the registered descriptor instead of any planner supplied metadata.

    ``calendar.create`` declares ``side_effect`` and ``requires_approval``, so it
    always pauses for approval; an unregistered capability is denied.
    """

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
        if descriptor.requires_approval:
            return PolicyDecision(action="require_approval", reason="capability requires approval")
        if descriptor.side_effect:
            return PolicyDecision(action="require_approval", reason="capability has an external side effect")
        return PolicyDecision(action="allow")
