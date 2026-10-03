"""Capability execution boundary for the Agent kernel."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.errors import classify_error
from app.kernel.execution_context import (
    ExecutionContext,
    bind_execution_context,
    current_conversation_context,
)
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
        """Plan/Step scoped fallback key.

        Used only when a Step carries no business key. Because a re-plan mints a
        new plan_id (and therefore a new step_id), this key is NOT stable across
        plan versions; external writes must supply ``arguments["idempotency_key"]``
        so the key survives replanning.
        """

        return f"{task_id}|{plan_id}|{step_id}"

    @staticmethod
    def business_key(step: PlanStep) -> str | None:
        """The Step declared stable identity of its external effect, if any."""

        value = (step.arguments or {}).get("idempotency_key")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def call_key(cls, task_id: str, plan_id: str, step_id: str, step: PlanStep) -> str:
        """Key of the CapabilityCall row for this Step.

        A business key wins: it is what makes a re-planned retry of the same
        external effect reuse one row (and one ``request_id``) instead of
        creating a second one.
        """

        business = cls.business_key(step)
        if business is not None:
            return f"business:{business}"
        return cls.idempotency_key(task_id, plan_id, step_id)

    def execute(
        self, task: TaskRecord, plan: Plan, step: PlanStep, attempt: int
    ) -> CapabilityCallRecord:
        business = self.business_key(step)
        key = self.call_key(task.task_id, plan.plan_id, step.step_id, step)
        existing = self.store.get_capability_call(key)
        if existing is not None:
            # A finished call is never repeated. An unknown external result must
            # not be re-issued blindly either: the same Step id means it is the
            # same attempt, and the Planner has to query or fail instead.
            # A call that merely paused for user input (missing time, unbound
            # calendar) wrote nothing, so it must be re-issued once the user
            # supplies the input.
            paused = bool((existing.result or {}).get("requires_user_input"))
            if existing.status == "succeeded" and not paused:
                return existing
            if existing.status == "unknown" and existing.step_id == step.step_id:
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
            # The external system deduplicates on this value, so it must be the
            # business key the Capability actually sends.
            request_id=existing.request_id if existing else (business or str(uuid4())),
            attempt=attempt,
            status="running",
            arguments=dict(step.arguments),
            created_at=existing.created_at if existing else started,
        )
        self.store.save_capability_call(call)

        try:
            arguments = capability.validate(step.arguments)
            execution_context = ExecutionContext.from_task(
                task,
                plan,
                step,
                request_id=call.request_id,
                conversation_context=current_conversation_context(),
            )
            with bind_execution_context(execution_context):
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

        call.finished_at = _now()
        # A capability that blew its own budget is a failure even though it
        # returned: the descriptor is a contract, and silently accepting an
        # over-budget call is how a slow dependency turns into a hung Task.
        # ``retryable_error`` hands it to the kernel's in-place retry.
        if (
            call.finished_at - started
        ).total_seconds() > capability.descriptor.timeout_seconds:
            call.status = "failed"
            call.error = {
                "classification": "retryable_error",
                "type": "CapabilityTimeout",
                "message": "capability exceeded its timeout budget",
            }
        else:
            call.status = "succeeded"
            call.result = output
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

    @staticmethod
    def preflight_observation(
        task: TaskRecord, plan: Plan, step: PlanStep, output: dict[str, Any]
    ) -> Observation:
        """Observation for a capability that paused before any external call.

        A preflight block happens before approval, so there is no CapabilityCall
        to convert. The Observation still carries the ``requires_user_input``
        payload that the API and clients read.
        """

        return Observation(
            observation_id=str(uuid4()),
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            capability=step.capability,
            status="succeeded",
            output=dict(output),
            created_at=_now(),
        )

    @staticmethod
    def failure_observation(
        task: TaskRecord, plan: Plan, step: PlanStep, error: dict[str, Any]
    ) -> Observation:
        """Observation for a Step the kernel refused before it was called.

        The executor's own failures are converted from the CapabilityCall; this
        covers the checks the kernel runs first (argument schema, unresolved
        references). They never reach a call, yet the Planner still has to see
        them as a failed Observation so it can repair the Step.
        """

        return Observation(
            observation_id=str(uuid4()),
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            capability=step.capability,
            status="failed",
            error=dict(error),
            created_at=_now(),
        )
