"""AgentRuntime: the controlled, resumable, sequential execution loop."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from app.kernel.checkpoint import all_steps_finished, build_checkpoint, next_pending_step
from app.kernel.errors import AgentContractError, ContractValidationError, TaskNotFoundError
from app.kernel.events import new_outbox_event, new_task_event, utcnow
from app.kernel.executor import CapabilityExecutor
from app.kernel.limits import ExecutionLimits
from app.kernel.models import TaskRecord, TaskRunResult
from app.kernel.protocols import AgentStore, Capability as CapabilityProtocol, Planner, PolicyEngine, TaskUnderstandingProvider
from app.kernel.registry import CapabilityRegistry
from app.kernel.states import (
    EVENT_PLAN_CREATED,
    EVENT_STEP_FAILED,
    EVENT_STEP_STARTED,
    EVENT_STEP_SUCCEEDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_PLANNING,
    EVENT_TASK_RETRYING,
    EVENT_TASK_WAITING_INPUT,
    EVENT_TASK_WAITING_APPROVAL,
    EVENT_APPROVAL_APPROVED,
    TERMINAL_TASK_STATUSES,
    WAITING_TASK_STATUSES,
    ensure_plan_transition,
    ensure_step_transition,
    ensure_task_transition,
)
from app.kernel.validator import PlanValidator


class AgentRuntime:
    """Runs one Task to completion, to a waiting state, or to a terminal failure.

    The runtime is storage agnostic: PostgreSQL is the production store while
    tests use the in-memory store. It never executes a capability directly; all
    execution flows through :class:`CapabilityExecutor`.
    """

    def __init__(
        self,
        *,
        store: AgentStore,
        registry: CapabilityRegistry,
        planner: Planner,
        policy: PolicyEngine,
        executor: CapabilityExecutor | None = None,
        approval_gateway=None,
        validator: PlanValidator | None = None,
        limits: ExecutionLimits | None = None,
        understanding_provider: TaskUnderstandingProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.planner = planner
        self.policy = policy
        self.limits = limits or ExecutionLimits()
        self.executor = executor or CapabilityExecutor(registry, store)
        self.approval_gateway = approval_gateway
        self.validator = validator or PlanValidator(registry, max_steps=self.limits.max_steps)
        self.understanding_provider = understanding_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep

    def run_task(self, task_id: str, *, lease_owner: str = "worker") -> TaskRunResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"unknown task: {task_id}")
        if task.status in TERMINAL_TASK_STATUSES or task.status in WAITING_TASK_STATUSES:
            return self._result(task, waiting_for=task.status if task.status in WAITING_TASK_STATUSES else None)
        if not self.store.acquire_lease(task_id, lease_owner, self.limits.lease_seconds):
            return self._result(task, waiting_for="lease")
        try:
            return self._drive(task_id)
        finally:
            self.store.release_lease(task_id, lease_owner)

    # -- internal ---------------------------------------------------------

    def _drive(self, task_id: str) -> TaskRunResult:
        executed: list[str] = []
        deadline_seconds = self.limits.max_execution_seconds
        # The execution budget covers one drive, not the Task's whole lifetime:
        # a Task may legitimately wait hours for approval or user input, and
        # that wait must not consume the budget of the resolving drive.
        started_at = self._clock()
        guard = (self.limits.max_steps * max(self.limits.max_step_attempts, 1)) + self.limits.max_steps + 5

        for _ in range(guard):
            task = self.store.get_task(task_id)
            if task is None:
                raise TaskNotFoundError(f"unknown task: {task_id}")
            if task.status in TERMINAL_TASK_STATUSES or task.status in WAITING_TASK_STATUSES:
                return self._result(task, executed=executed, waiting_for=task.status if task.status in WAITING_TASK_STATUSES else None)

            if (self._clock() - started_at).total_seconds() > deadline_seconds:
                return self._fail(task, {"classification": "permanent_error", "message": "execution time limit exceeded"}, executed=executed)

            plan = self.store.get_active_plan(task.task_id) if task.current_plan_id else None
            if plan is None:
                planned = self._plan(task)
                if isinstance(planned, TaskRunResult):
                    planned.executed_step_ids = executed
                    return planned
                continue

            if plan.status in {"draft", "validated"}:
                ensure_plan_transition(plan.status, "running")
                plan.status = "running"
                self.store.save_plan(plan)

            steps = self.store.list_steps(plan.plan_id)
            step = next_pending_step(steps)
            if step is None:
                if not steps:
                    # A zero-step plan means the request carried nothing this
                    # Agent can act on: that is a completion, not a failure.
                    return self._complete(task, plan, steps, executed)
                if all_steps_finished(steps):
                    return self._complete(task, plan, steps, executed)
                return self._fail(
                    task,
                    {"classification": "permanent_error", "message": "plan has no executable step"},
                    executed=executed,
                )

            try:
                self.validator.validate_step(plan, step, task.to_envelope())
            except ContractValidationError as exc:
                return self._fail(
                    task,
                    {"classification": "validation_error", "message": str(exc), "errors": exc.errors},
                    executed=executed,
                )

            capability = self.registry.get(step.capability)
            decision = self.policy.evaluate(task.to_envelope(), step, capability.descriptor)
            approved = self._approved_approval(task, step)

            if decision.action == "deny":
                self._mark_step(
                    step,
                    "failed",
                    last_error={"classification": "policy_denied", "message": decision.reason},
                )
                return self._fail(
                    task,
                    {"classification": "policy_denied", "message": decision.reason or "policy denied"},
                    executed=executed,
                    plan=plan,
                    steps=self.store.list_steps(plan.plan_id),
                    step_event=True,
                    step_id=step.step_id,
                )

            # Policy still wins on deny, but an already approved step must run
            # with the user-confirmed arguments instead of pausing again.
            if decision.action == "require_approval" and approved is None:
                return self._wait_for_approval(task, plan, step, decision.reason, executed)

            outcome = self._execute_step(task, plan, step, executed)
            if outcome is not None:
                return outcome
        return self._fail(
            self.store.get_task(task_id),
            {"classification": "permanent_error", "message": "execution loop guard exceeded"},
            executed=executed,
        )

    def _plan(self, task: TaskRecord):
        envelope = task.to_envelope()
        ensure_task_transition(task.status, "planning")
        task.status = "planning"
        task.updated_at = utcnow()
        self.store.commit(task, events=[new_task_event(task.task_id, EVENT_TASK_PLANNING, {"objective": task.objective})])

        if self.understanding_provider is not None:
            self.understanding_provider.understand(envelope)

        plan = self.planner.create_plan(
            envelope, self.registry.list_descriptors(), self.store.list_observations(task.task_id)
        )
        try:
            self.validator.validate(plan, envelope)
        except ContractValidationError as exc:
            return self._fail(
                task,
                {"classification": "validation_error", "message": str(exc), "errors": exc.errors},
            )

        plan.status = "validated"
        self.store.invalidate_plans(task.task_id)
        self.store.save_plan(plan)
        self.store.save_steps(task.task_id, plan.steps)

        task = self.store.get_task(task.task_id)
        task.current_plan_id = plan.plan_id
        task.current_plan_version = plan.version
        task.objective = plan.objective
        ensure_task_transition(task.status, "ready")
        task.status = "ready"
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_PLAN_CREATED,
                    {"plan_id": plan.plan_id, "version": plan.version, "steps": [step.step_id for step in plan.steps]},
                )
            ],
        )
        return None

    def _approved_approval(self, task: TaskRecord, step):
        approvals = [
            item
            for item in self.store.list_approvals(task_id=task.task_id)
            if item.step_id == step.step_id and item.status == "approved"
        ]
        if not approvals:
            return None
        approvals.sort(key=lambda item: item.version)
        return approvals[-1]

    def _execute_step(self, task: TaskRecord, plan, step, executed: list[str]):
        attempt = step.attempt_count + 1
        if attempt > self.limits.max_step_attempts:
            self._mark_step(step, "failed", last_error={"classification": "permanent_error", "message": "step attempt limit exceeded"})
            return self._fail(
                task,
                {"classification": "permanent_error", "message": "step attempt limit exceeded"},
                executed=executed,
                plan=plan,
                steps=self.store.list_steps(plan.plan_id),
            )

        if step.status == "pending":
            self._mark_step(step, "ready")
        ensure_step_transition(step.status, "running")
        step.status = "running"
        self.store.update_step(step, attempt_count=attempt)

        task = self.store.get_task(task.task_id)
        ensure_task_transition(task.status, "executing")
        task.status = "executing"
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_STEP_STARTED,
                    {"step_id": step.step_id, "capability": step.capability, "attempt": attempt},
                )
            ],
        )

        call = self.executor.execute(task, plan, step, attempt)
        observation = self.executor.to_observation(call)
        self.store.save_observation(observation)

        if call.status == "succeeded":
            output = call.result or {}
            if output.get("requires_user_input"):
                step.status = "ready"
                self.store.update_step(step)
                task = self.store.get_task(task.task_id)
                ensure_task_transition(task.status, "waiting_input")
                task.status = "waiting_input"
                task.updated_at = utcnow()
                self.store.commit(
                    task,
                    events=[
                        new_task_event(
                            task.task_id,
                            EVENT_TASK_WAITING_INPUT,
                            {
                                "step_id": step.step_id,
                                "capability": step.capability,
                                "missing_information": output.get("missing_information", []),
                            },
                        )
                    ],
                )
                return self._result(task, executed=executed, waiting_for="waiting_input")

            step.status = "succeeded"
            self.store.update_step(step)
            task = self.store.get_task(task.task_id)
            steps = self.store.list_steps(plan.plan_id)
            task.checkpoint = build_checkpoint(task, plan, steps).model_dump(mode="json")
            task.updated_at = utcnow()
            self.store.commit(
                task,
                events=[
                    new_task_event(
                        task.task_id,
                        EVENT_STEP_SUCCEEDED,
                        {"step_id": step.step_id, "capability": step.capability, "observation_id": observation.observation_id},
                    )
                ],
            )
            executed.append(step.step_id)
            return None

        error = call.error or {"classification": "permanent_error", "message": "capability failed"}
        classification = error.get("classification")

        if classification == "unknown_external_result":
            step.status = "unknown"
            self.store.update_step(step, last_error=error)
            return self._fail(
                task,
                error,
                executed=executed,
                plan=plan,
                steps=self.store.list_steps(plan.plan_id),
                step_event=True,
                step_id=step.step_id,
                terminal_status="unknown",
            )

        if classification == "retryable_error" and attempt < self.limits.max_step_attempts:
            step.status = "ready"
            self.store.update_step(step, attempt_count=attempt, last_error=error)
            task = self.store.get_task(task.task_id)
            ensure_task_transition(task.status, "ready")
            task.status = "ready"
            task.updated_at = utcnow()
            self.store.commit(
                task,
                events=[
                    new_task_event(
                        task.task_id,
                        EVENT_TASK_RETRYING,
                        {"step_id": step.step_id, "attempt": attempt, "error": error},
                    )
                ],
                outbox_events=[
                    new_outbox_event(task.task_id, "agent.task.wakeup", {"reason": "retry"})
                ],
            )
            backoff = self.limits.backoff_for(attempt)
            if backoff > 0:
                self._sleep(backoff)
            return None

        self._mark_step(step, "failed", last_error=error)
        return self._fail(
            task,
            error,
            executed=executed,
            plan=plan,
            steps=self.store.list_steps(plan.plan_id),
            step_event=True,
            step_id=step.step_id,
        )

    def _wait_for_approval(self, task: TaskRecord, plan, step, reason, executed: list[str]) -> TaskRunResult:
        if self.approval_gateway is None:
            return self._fail(
                task,
                {"classification": "policy_denied", "message": "approval required but no approval gateway configured"},
                executed=executed,
            )
        approval = self.approval_gateway.request(task, step, reason=reason, version=step.attempt_count + 1)
        ensure_step_transition(step.status, "waiting_approval")
        step.status = "waiting_approval"
        self.store.update_step(step, approved_version=approval.version)

        task = self.store.get_task(task.task_id)
        ensure_task_transition(task.status, "waiting_approval")
        task.status = "waiting_approval"
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_TASK_WAITING_APPROVAL,
                    {
                        "approval_id": approval.approval_id,
                        "step_id": step.step_id,
                        "capability": step.capability,
                        "arguments": approval.arguments,
                        "version": approval.version,
                    },
                )
            ],
        )
        result = self._result(task, executed=executed, waiting_for="waiting_approval")
        return result

    def resume_after_approval(self, task_id: str, *, lease_owner: str = "worker") -> TaskRunResult:
        """Continue a Task that was paused for approval."""

        return self.run_task(task_id, lease_owner=lease_owner)

    def _complete(self, task: TaskRecord, plan, steps, executed: list[str]) -> TaskRunResult:
        ensure_plan_transition(plan.status, "completed")
        plan.status = "completed"
        self.store.save_plan(plan)
        task = self.store.get_task(task.task_id)
        ensure_task_transition(task.status, "succeeded")
        task.status = "succeeded"
        task.checkpoint = build_checkpoint(task, plan, steps).model_dump(mode="json")
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_TASK_COMPLETED,
                    {"plan_id": plan.plan_id, "executed_steps": executed},
                )
            ],
        )
        return self._result(task, executed=executed)

    def _fail(
        self,
        task: TaskRecord,
        error: dict,
        *,
        executed: list[str] | None = None,
        plan=None,
        steps=None,
        step_event: bool = False,
        step_id: str | None = None,
        terminal_status: str = "failed",
    ) -> TaskRunResult:
        task = self.store.get_task(task.task_id)
        if plan is not None and plan.status in {"draft", "validated", "running"}:
            ensure_plan_transition(plan.status, "failed")
            plan.status = "failed"
            self.store.save_plan(plan)
        if steps:
            task.checkpoint = build_checkpoint(task, plan, steps).model_dump(mode="json")
        ensure_task_transition(task.status, terminal_status)
        task.status = terminal_status
        task.last_error = error
        task.updated_at = utcnow()
        events = []
        if step_event:
            events.append(
                new_task_event(
                    task.task_id,
                    EVENT_STEP_FAILED,
                    {"error": error, "step_id": step_id},
                )
            )
        events.append(
            new_task_event(
                task.task_id,
                EVENT_TASK_FAILED,
                {"error": error, "task_status": terminal_status},
            )
        )
        self.store.commit(task, events=events)
        return self._result(task, executed=executed or [])

    def _mark_step(self, step, status: str, *, last_error: dict | None = None) -> None:
        if step.status != status:
            ensure_step_transition(step.status, status)
            step.status = status
        self.store.update_step(step, last_error=last_error)

    def _result(
        self, task: TaskRecord, *, executed: list[str] | None = None, waiting_for: str | None = None
    ) -> TaskRunResult:
        return TaskRunResult(
            task_id=task.task_id,
            status=task.status,
            plan_id=task.current_plan_id,
            executed_step_ids=list(executed or []),
            waiting_for=waiting_for,
        )
