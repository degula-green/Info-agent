"""Capability execution boundary for the Agent kernel."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.kernel.errors import classify_error
from app.kernel.models import (
    CapabilityCallRecord,
    Observation,
    Plan,
    PlanStep,
    TaskRecord,
)
from app.kernel.protocols import AgentStore
from app.kernel.registry import CapabilityRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CapabilityExecutor:
    """Validates arguments, creates the CapabilityCall and normalizes results.

    Capabilities never mutate Task, Plan, Step or Approval state themselves.
    Repeated attempts reuse a deterministic idempotency key so a retry cannot
    duplicate an already successful call.
    """

    def __init__(self, registry: CapabilityRegistry, store: AgentStore) -> None:
        self.registry = registry
        self.store = store

    @staticmethod
    def idempotency_key(task_id: str, plan_id: str, step_id: str) -> str:
        """Stable for the whole Step, not per attempt.

        An external write must be deduplicated across retries, so every attempt
        of the same Step reuses one CapabilityCall and one ``request_id``.
        """

        return f"{task_id}|{plan_id}|{step_id}"

    def execute(
        self, task: TaskRecord, plan: Plan, step: PlanStep, attempt: int
    ) -> CapabilityCallRecord:
        key = self.idempotency_key(task.task_id, plan.plan_id, step.step_id)
        existing = self.store.get_capability_call(key)
        if existing is not None and existing.status == "succeeded":
            return existing

        capability = self.registry.get(step.capability)
        started = _now()
        call = CapabilityCallRecord(
            call_id=existing.call_id if existing else str(uuid4()),
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            capability=step.capability,
            idempotency_key=key,
            request_id=existing.request_id if existing else str(uuid4()),
            attempt=attempt,
            status="running",
            arguments=dict(step.arguments),
            created_at=existing.created_at if existing else started,
        )
        self.store.save_capability_call(call)

        try:
            arguments = capability.validate(step.arguments)
            output = capability.execute(arguments)
        except Exception as exc:  # noqa: BLE001 - normalized into a classified error
            classification = classify_error(exc)
            call.status = "unknown" if classification == "unknown_external_result" else "failed"
            call.error = {
                "classification": classification,
                "type": type(exc).__name__,
                "message": str(exc),
            }
            call.finished_at = _now()
            self.store.save_capability_call(call)
            return call

        call.status = "succeeded"
        call.result = output
        call.finished_at = _now()
        self.store.save_capability_call(call)
        return call

    @staticmethod
    def to_observation(call: CapabilityCallRecord) -> Observation:
        if call.status == "succeeded":
            return Observation(
                observation_id=str(uuid4()),
                task_id=call.task_id,
                plan_id=call.plan_id,
                step_id=call.step_id,
                capability=call.capability,
                status="succeeded",
                output=call.result,
                created_at=_now(),
            )
        status = "unknown" if call.status == "unknown" else "failed"
        return Observation(
            observation_id=str(uuid4()),
            task_id=call.task_id,
            plan_id=call.plan_id,
            step_id=call.step_id,
            capability=call.capability,
            status=status,
            error=call.error,
            created_at=_now(),
        )
