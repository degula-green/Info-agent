from __future__ import annotations


class AgentContractError(Exception):
    """Base error for contract-layer failures."""


class CapabilityNotFoundError(AgentContractError):
    def __init__(self, capability_name: str) -> None:
        self.capability_name = capability_name
        super().__init__(f"unknown capability: {capability_name}")


class DuplicateCapabilityError(AgentContractError):
    def __init__(self, capability_name: str) -> None:
        self.capability_name = capability_name
        super().__init__(f"capability already registered: {capability_name}")


class ContractValidationError(AgentContractError):
    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        self.errors = errors or [message]
        details = "; ".join(self.errors)
        super().__init__(f"{message}: {details}")


class InvalidStateTransitionError(AgentContractError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(f"invalid {entity} transition: {current} -> {target}")


class RetryableCapabilityError(AgentContractError):
    """Capability failure that may be retried with the same idempotency key."""

    classification = "retryable_error"


class PermanentCapabilityError(AgentContractError):
    """Capability failure that must not be retried."""

    classification = "permanent_error"


class TaskNotFoundError(AgentContractError):
    """The Task id has no persisted record; a wake-up for it must be dropped."""


class UnknownExternalResultError(AgentContractError):
    """The external call may have succeeded or failed; it must not be retried blindly."""

    classification = "unknown_external_result"


def classify_error(exc: BaseException) -> str:
    """Map an exception to the documented error classification."""

    classification = getattr(exc, "classification", None)
    if classification in {
        "validation_error",
        "policy_denied",
        "retryable_error",
        "permanent_error",
        "unknown_external_result",
    }:
        return classification
    if isinstance(exc, ContractValidationError):
        return "validation_error"
    try:
        from pydantic import ValidationError as PydanticValidationError

        if isinstance(exc, PydanticValidationError):
            return "validation_error"
    except ImportError:  # pragma: no cover - pydantic is a hard dependency
        pass
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "retryable_error"
    return "permanent_error"
