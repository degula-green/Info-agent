from __future__ import annotations

from collections.abc import Iterable

from app.kernel.errors import (
    CapabilityNotFoundError,
    ContractValidationError,
    DuplicateCapabilityError,
)
from app.kernel.protocols import Capability
from app.kernel.models import CapabilityDescriptor


class CapabilityRegistry:
    """Trusted source for registered capabilities and their descriptors."""

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        self._capabilities: dict[str, Capability] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: Capability) -> None:
        descriptor = capability.descriptor
        name = descriptor.name
        if name in self._capabilities:
            raise DuplicateCapabilityError(name)
        # A write whose outcome cannot be read back must be authorised before it
        # happens. Deciding this at registration time means the question is
        # answered when the capability is written, not when it is already in
        # flight and nobody can tell whether the external effect landed.
        if (
            descriptor.side_effect
            and not descriptor.reconcilable
            and not descriptor.requires_approval
        ):
            raise ContractValidationError(
                f"{name}: 有副作用又无法事后对账的能力，必须要求审批"
            )
        self._capabilities[name] = capability

    def get(self, name: str) -> Capability:
        try:
            return self._capabilities[name]
        except KeyError as exc:
            raise CapabilityNotFoundError(name) from exc

    def find(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def list_descriptors(self) -> list[CapabilityDescriptor]:
        return [capability.descriptor.model_copy(deep=True) for capability in self._capabilities.values()]
