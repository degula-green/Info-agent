"""AgentRuntime: a controlled, resumable, sequential execution loop."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.kernel.bindings import bind_plan_references
from app.kernel.checkpoint import build_checkpoint, next_pending_step, resume_step
from app.kernel.errors import ContractValidationError, TaskNotFoundError, classify_error
from app.kernel.events import new_task_event, utcnow
from app.kernel.executor import CapabilityExecutor
from app.kernel.limits import ExecutionLimits
from app.kernel.models import (
    EvidenceRecord,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    PlanStep,
    TaskRecord,
    TaskRunResult,
    TaskUnderstanding,
)
from app.kernel.protocols import (
    AgentStore,
    Capability as CapabilityProtocol,
    Planner,
    PolicyEngine,
    TaskUnderstandingProvider,
)
from app.kernel.references import (
    ReferenceResolutionError,
    has_references,
    resolve_arguments,
)
from app.kernel.registry import CapabilityRegistry
from app.kernel.states import (
    EVENT_PLAN_CREATED,
    EVENT_PREVIEW_CONFIRMED,
    EVENT_PLAN_REPLANNED,
    EVENT_PLANNER_DECISION,
    EVENT_STEP_FAILED,
    EVENT_STEP_STARTED,
    EVENT_STEP_SUCCEEDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_PLANNING,
    EVENT_TASK_UNDERSTANDING,
    EVENT_TASK_WAITING_APPROVAL,
    EVENT_TASK_WAITING_INPUT,
    TERMINAL_TASK_STATUSES,
    WAITING_TASK_STATUSES,
    ensure_plan_transition,
    ensure_step_transition,
    ensure_task_transition,
)
from app.kernel.validator import PlanValidator
from app.kernel.approval import approval_matches_arguments

logger = logging.getLogger("agent.runtime")


def _describe_arguments(arguments: dict | None) -> str:
    """A short, stable rendering of Step arguments for an error message."""

    text = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + "..."


class AgentRuntime:
    """Runs one Task to completion, a waiting state, or a terminal state."""

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
        understanding_mode: str = "off",
        settings = None,
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
        self.validator = validator or PlanValidator(
            registry, max_steps=self.limits.max_steps
        )
        self.understanding_provider = understanding_provider
        self.understanding_mode = (understanding_mode or "off").strip().lower()
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep

    def run_task(self, task_id: str, *, lease_owner: str = "worker") -> TaskRunResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"unknown task: {task_id}")
        if task.status in TERMINAL_TASK_STATUSES or task.status in WAITING_TASK_STATUSES:
            return self._result(
                task,
                waiting_for=task.status if task.status in WAITING_TASK_STATUSES else None,
            )
        if not self.store.acquire_lease(task_id, lease_owner, self.limits.lease_seconds):
            return self._result(task, waiting_for="lease")
        try:
            return self._drive(task_id)
        finally:
            self.store.release_lease(task_id, lease_owner)

    # -- main loop --------------------------------------------------------

    def _drive(self, task_id: str) -> TaskRunResult:
        executed: list[str] = []
        started_at = self._clock()
        guard = (
            self.limits.max_steps * max(self.limits.max_step_attempts, 1)
            + self.limits.max_replans
            + self.limits.max_steps
            + 10
        )

        # 【附件预处理】在Understanding之前增强上下文
        first_task = self.store.get_task(task_id)
        if first_task and first_task.input.get("attachment_ids") and not first_task.input.get("_attachment_processed"):
            self._preprocess_attachments(first_task)

        for _ in range(guard):
            task = self.store.get_task(task_id)
            if task is None:
                raise TaskNotFoundError(f"unknown task: {task_id}")
            if task.status in TERMINAL_TASK_STATUSES or task.status in WAITING_TASK_STATUSES:
                return self._result(
                    task,
                    executed=executed,
                    waiting_for=task.status if task.status in WAITING_TASK_STATUSES else None,
                )
            if (self._clock() - started_at).total_seconds() > self.limits.max_execution_seconds:
                return self._fail(
                    task,
                    {
                        "classification": "permanent_error",
                        "message": "execution time limit exceeded",
                    },
                    executed=executed,
                )

            plan = self.store.get_active_plan(task.task_id) if task.current_plan_id else None
            if plan is None:
                planning = self._begin_planning(task)
                if isinstance(planning, TaskRunResult):
                    planning.executed_step_ids = executed
                    return planning
                understanding = self._prepare_understanding(task)
                if isinstance(understanding, TaskRunResult):
                    understanding.executed_step_ids = executed
                    return understanding
                planned = self._plan(task, understanding)
                if isinstance(planned, TaskRunResult):
                    planned.executed_step_ids = executed
                    return planned
                continue

            understanding = self._load_understanding(task)
            if plan.requires_user_confirmation:
                return self._pause_for_input(
                    task,
                    plan,
                    None,
                    {
                        "requires_user_input": True,
                        "missing_information": ["partial_execution_confirmation"],
                        "unsupported_intents": list(plan.unsupported_intents),
                        "reason": "partial execution requires user confirmation",
                    },
                    executed,
                )
            if plan.status in {"draft", "validated"}:
                ensure_plan_transition(plan.status, "running")
                plan.status = "running"
                self.store.save_plan(plan)

            steps = self.store.list_steps(plan.plan_id)
            # A Step left "running" by a crashed worker is unfinished work, not
            # finished work: falling through to next_pending_step would skip its
            # external effect entirely.
            step = resume_step(steps)
            observations = self.store.list_observations(task.task_id)

            if step is None:
                decision_result = self._decide_and_apply(
                    task,
                    plan,
                    observations,
                    understanding,
                    executed,
                    no_pending_step=True,
                )
                if decision_result is not None:
                    return decision_result
                continue

            if step.status == "running":
                recovered = self._recover_running_step(task, plan, step, executed)
                if recovered is not None:
                    return recovered
                continue

            try:
                resolved_arguments = resolve_arguments(step.arguments, observations)
            except ReferenceResolutionError as exc:
                output = {
                    "requires_user_input": True,
                    "missing_information": [f"reference:{exc.reference}"],
                    "reason": str(exc),
                }
                observation = self.executor.preflight_observation(task, plan, step, output)
                self.store.save_observation(observation)
                decision_result = self._decide_and_apply(
                    task,
                    plan,
                    self.store.list_observations(task.task_id),
                    understanding,
                    executed,
                )
                if decision_result is not None:
                    return decision_result
                continue
            if resolved_arguments != step.arguments:
                step.arguments = resolved_arguments
                self.store.update_step(step)

            try:
                self.validator.validate_step(plan, step, task.to_envelope())
            except ContractValidationError as exc:
                # A schema mismatch is the Planner's own output being wrong, so
                # it goes back to the Planner to repair -- the same treatment the
                # capability's own validation gets inside the Executor. Failing
                # the Task here hid a repairable Plan behind a hard stop.
                error = {
                    "classification": "validation_error",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "errors": exc.errors,
                }
                self._mark_step(step, "failed", last_error=error)
                self.store.save_observation(
                    self.executor.failure_observation(task, plan, step, error)
                )
                decision_result = self._decide_and_apply(
                    task,
                    plan,
                    self.store.list_observations(task.task_id),
                    understanding,
                    executed,
                )
                if decision_result is not None:
                    return decision_result
                continue

            capability = self.registry.get(step.capability)
            # The limit protects against looping on one tool. It counts an
            # unbroken run of the same capability rather than every use of it:
            # "search, fetch the hit, search the next question" is a normal
            # research path, while "search the same thing again and again" is a
            # loop. A deliberately planned first Plan is exempt either way.
            repeated_run = self._repeated_capability_run(step, observations)
            if (
                task.replan_count > 0
                and repeated_run >= self.limits.max_same_capability_calls
            ):
                return self._fail(
                    task,
                    {
                        "classification": "permanent_error",
                        "message": (
                            f"这一步已经连续尝试 {repeated_run} 次仍未成功，"
                            f"已停止（{step.capability}）"
                        ),
                    },
                    executed=executed,
                    plan=plan,
                    steps=steps,
                )
            policy = self.policy.evaluate(task.to_envelope(), step, capability.descriptor)
            approved = self._approved_approval(task, step)

            if policy.action == "deny":
                self._mark_step(
                    step,
                    "failed",
                    last_error={
                        "classification": "policy_denied",
                        "message": policy.reason,
                    },
                )
                return self._fail(
                    task,
                    {
                        "classification": "policy_denied",
                        "message": policy.reason or "policy denied",
                    },
                    executed=executed,
                    plan=plan,
                    steps=steps,
                    step_event=True,
                    step_id=step.step_id,
                )

            waiting = self._preflight(
                task,
                plan,
                step,
                capability,
                understanding,
                executed,
            )
            if waiting is not None:
                return waiting

            if policy.action == "require_approval" and approved is None:
                return self._wait_for_approval(
                    task, plan, step, policy.reason, executed
                )

            executed_outcome = self._execute_step(
                task,
                plan,
                step,
                understanding,
                executed,
            )
            if executed_outcome is not None:
                return executed_outcome

        return self._fail(
            self.store.get_task(task_id),
            {
                "classification": "permanent_error",
                "message": "execution loop guard exceeded",
            },
            executed=executed,
        )

    # -- understanding ----------------------------------------------------

    def _load_understanding(self, task: TaskRecord) -> TaskUnderstanding | None:
        if self.understanding_mode != "enforce" or not task.understanding:
            return None
        try:
            return TaskUnderstanding.model_validate(task.understanding)
        except Exception:  # noqa: BLE001 - malformed persisted state is not actionable
            return None

    def _prepare_understanding(
        self, task: TaskRecord
    ) -> TaskUnderstanding | TaskRunResult | None:
        if self.understanding_mode == "off":
            return None
        if self.understanding_mode not in {"shadow", "enforce"}:
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": f"invalid understanding mode: {self.understanding_mode}",
                },
            )
        if self.understanding_mode == "enforce" and task.understanding:
            return self._load_understanding(task)
        if self.understanding_provider is None:
            if self.understanding_mode == "shadow":
                return None
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "understanding provider is not configured",
                },
            )

        started = self._clock()
        provider_name = getattr(
            self.understanding_provider,
            "name",
            type(self.understanding_provider).__name__,
        )
        # Model-backed providers can declare their worst expected call count.
        # Hybrid providers start with Laya and may add an LLM fallback later.
        estimated_calls = max(
            int(getattr(self.understanding_provider, "estimated_model_calls", 0)),
            0,
        )
        modeled = bool(
            getattr(
                self.understanding_provider,
                "model_backed",
                provider_name == "llm",
            )
        )
        if not self._can_spend_model_calls(task.task_id, estimated_calls):
            if self.understanding_mode == "shadow":
                self._append_event(
                    task,
                    EVENT_TASK_UNDERSTANDING,
                    {
                        "mode": self.understanding_mode,
                        "provider": provider_name,
                        "skipped": "model_call_budget_exceeded",
                    },
                )
                return None
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "model call budget exceeded",
                },
            )
        try:
            understanding = self._call_understanding_provider(task)
        except Exception as exc:  # noqa: BLE001 - provider errors are classified
            error = {
                "classification": classify_error(exc),
                "type": type(exc).__name__,
                "message": str(exc),
            }
            self._append_event(
                task,
                EVENT_TASK_UNDERSTANDING,
                {
                    "mode": self.understanding_mode,
                    "provider": provider_name,
                    "model": getattr(self.understanding_provider, "model", None),
                    "latency_ms": int((self._clock() - started).total_seconds() * 1000),
                    "text": str(task.input.get("text") or ""),
                    "provider_error": error,
                },
            )
            if self.understanding_mode == "shadow":
                return None
            return self._fail(task, error)
        if not isinstance(understanding, TaskUnderstanding):
            error = {
                "classification": "permanent_error",
                "type": type(understanding).__name__,
                "message": "understanding provider returned an invalid result",
            }
            self._append_event(
                task,
                EVENT_TASK_UNDERSTANDING,
                {
                    "mode": self.understanding_mode,
                    "provider": provider_name,
                    "text": str(task.input.get("text") or ""),
                    "provider_error": error,
                },
            )
            if self.understanding_mode == "shadow":
                return None
            return self._fail(task, error)

        actual_calls = max(
            int(getattr(self.understanding_provider, "last_call_count", 0)),
            0,
        )
        if not modeled:
            actual_calls = 0
        if not self._can_spend_model_calls(task.task_id, actual_calls):
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "model call budget exceeded",
                },
            )
        self._reserve_model_calls(task.task_id, actual_calls)
        payload = {
            "mode": self.understanding_mode,
            "provider": provider_name,
            "model": getattr(self.understanding_provider, "model", None),
            "decision_source": getattr(
                self.understanding_provider, "last_decision_source", None
            ),
            "fallback_reason": getattr(
                self.understanding_provider, "last_fallback_reason", None
            ),
            "laya_answer_confidence": getattr(
                self.understanding_provider, "last_answer_confidence", None
            ),
            "laya_margin": getattr(
                self.understanding_provider, "last_margin", None
            ),
            "latency_ms": int((self._clock() - started).total_seconds() * 1000),
            "text": str(task.input.get("text") or ""),
            "understanding": understanding.model_dump(mode="json"),
        }
        if self.understanding_mode == "shadow":
            self._append_event(task, EVENT_TASK_UNDERSTANDING, payload)
            return None

        task = self.store.get_task(task.task_id)
        task.understanding = understanding.model_dump(mode="json")
        self.store.commit(
            task,
            events=[new_task_event(task.task_id, EVENT_TASK_UNDERSTANDING, payload)],
        )
        return understanding

    # -- planning ---------------------------------------------------------

    def _preview_event(self, task: TaskRecord) -> dict | None:
        """The ``task.preview_confirmed`` payload when a draft became a real row.

        The draft a client shows is the Task's Plan plus its Approval; the to-do
        is a ledger row with a different lifetime. This is built at the moment a
        write Step succeeds, which is the only moment the row starts existing --
        announcing it any earlier would tell the client that a draft it is still
        showing has already become something it has not.

        Whether the row exists is asserted from the Observation itself: a
        rejected or unexecutable draft must not claim one was created.
        """

        created = [
            observation
            for observation in self.store.list_observations(task.task_id)
            if observation.status == "succeeded"
            and (observation.output or {}).get("todo_id")
        ]
        if not created:
            return None
        latest = created[-1]
        output = latest.output or {}
        return {
            "todo_id": output.get("todo_id"),
            "title": output.get("title"),
            "due_at": output.get("due_at"),
            "due_expression": output.get("due_expression"),
            "step_id": latest.step_id,
        }

    def _capture_evidence(self, observation: Observation) -> int:
        """Persists the provenance a capability returned; returns its model spend.

        Evidence stays in the observation output as well, because a later step
        reads it with $steps.<step_id>.output.evidence -- that is how
        answer.compose receives the material it has to cite. The stored row id is
        namespaced by observation: two Tasks fetching the same page produce the
        same logical evidence_id, and the table key must not collide across them.
        """

        output = observation.output or {}
        evidence = [
            dict(item)
            for item in (output.get("evidence") or [])
            if isinstance(item, dict)
        ]
        observation.evidence = evidence
        for item in evidence:
            logical_id = str(item.get("evidence_id") or "").strip()
            if not logical_id:
                continue
            self.store.save_evidence(
                EvidenceRecord(
                    evidence_id=f"{observation.observation_id}:{logical_id}",
                    task_id=observation.task_id,
                    plan_id=observation.plan_id,
                    step_id=observation.step_id,
                    observation_id=observation.observation_id,
                    payload=item,
                    created_at=utcnow(),
                )
            )
        try:
            return max(int(output.get("model_calls") or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _charge_model_calls(self, task_id: str, count: int) -> None:
        """Adds a capability own model spend to the Task budget.

        The spend already happened, so this cannot roll it back; it only makes
        the remaining budget honest for the calls that come after it.
        """

        if count <= 0:
            return
        task = self.store.get_task(task_id)
        if task is None:
            return
        task.model_call_count += int(count)
        task.updated_at = utcnow()
        self.store.commit(task)

    def _answer_payload(self, task: TaskRecord) -> dict | None:
        """The answer a finished Task produced, if any step produced one.

        Same shape as the preview rule: a successful Observation whose output
        carries an ``answer`` is the finished work, whatever capability wrote it.
        """

        produced = [
            observation
            for observation in self.store.list_observations(task.task_id)
            if observation.status == "succeeded"
            and (observation.output or {}).get("answer")
        ]
        if not produced:
            return None
        latest = produced[-1]
        output = latest.output or {}
        return {
            "answer": output.get("answer"),
            "citations": output.get("citations") or [],
        }

    # -- planning ---------------------------------------------------------

    def _call_understanding_provider(self, task: TaskRecord) -> TaskUnderstanding:
        """Runs the provider with the threshold that matches the ingress.

        Collected messages are noisier than typed chat, so they carry a higher
        confidence bar. Providers that do not accept an override (including the
        test doubles) are called with the envelope only.
        """

        envelope = task.to_envelope()
        # Intent comes from the user's own sentence. The enriched text keeps
        # the parsed document for planning, but a 30K attachment body would
        # otherwise dilute the classifier's signal.
        original_text = task.input.get("_original_text")
        if isinstance(original_text, str) and original_text.strip():
            envelope.input["text"] = original_text
        understand = self.understanding_provider.understand
        threshold = self._understanding_threshold(task)
        if threshold is None:
            return understand(envelope)
        try:
            return understand(envelope, min_confidence=threshold)
        except TypeError:
            return understand(envelope)

    def _understanding_threshold(self, task: TaskRecord) -> float | None:
        collected = self.limits.understanding_min_confidence_collected
        chat = self.limits.understanding_min_confidence
        if task.source_type == "knowledge_event":
            return collected if collected is not None else chat
        return chat

    def _begin_planning(self, task: TaskRecord) -> TaskRunResult | None:
        ensure_task_transition(task.status, "planning")
        task.status = "planning"
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_TASK_PLANNING,
                    {"objective": task.objective},
                )
            ],
        )
        return None

    def _planner_call(self, call, *, attempts: int = 2):
        """Runs one Planner call, retrying transient transport failures.

        The Planner reaches a remote model through whatever network the host
        has. A single dropped connection is not a reason to fail a Task -- and
        when it happens on the closing call, it is not a reason to discard work
        that already succeeded -- so a retryable failure is retried a couple of
        times before it is reported.
        """

        attempt = 0
        limit = max(1, int(attempts))
        while True:
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - classified below
                attempt += 1
                if classify_error(exc) != "retryable_error" or attempt >= limit:
                    raise

    def _plan(
        self, task: TaskRecord, understanding: TaskUnderstanding | None
    ) -> Plan | TaskRunResult:
        planner_calls = self._planner_max_calls()
        planner_calls = self._planner_calls_for_budget()
        if not self._can_spend_model_calls(task.task_id, planner_calls):
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "model call budget exceeded",
                },
            )
        try:
            plan = self._planner_call(
                lambda: self.planner.create_plan(
                    task.to_envelope(),
                    self.registry.list_descriptors(),
                    self.store.list_observations(task.task_id),
                    self._planning_constraints(),
                    understanding,
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize planner failures
            return self._fail(
                task,
                {
                    "classification": classify_error(exc),
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        self._reserve_model_calls(
            task.task_id,
            self._planner_calls_actually_spent(),
        )
        try:
            # The LLM planner binds before returning. A Plan that arrives from
            # anywhere else (an older version, a test double, a future planner)
            # gets the same normalization here, so the Runtime never has to
            # interpret a planner-facing reference object as an argument.
            plan = bind_plan_references(plan, self.registry.list_descriptors())
            self.validator.validate(plan, task.to_envelope())
        except ContractValidationError as exc:
            return self._fail(
                task,
                {
                    "classification": "validation_error",
                    "message": str(exc),
                    "errors": exc.errors,
                },
            )

        plan.version = self.store.next_plan_version(task.task_id)
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
                    {
                        "plan_id": plan.plan_id,
                        "version": plan.version,
                        "steps": [step.step_id for step in plan.steps],
                    },
                )
            ],
        )
        return plan

    def _planning_constraints(self) -> PlanningConstraints:
        return PlanningConstraints(
            max_steps=self.limits.max_steps,
            max_replans=self.limits.max_replans,
            max_model_calls=self.limits.max_model_calls,
            max_runtime_seconds=self.limits.max_execution_seconds,
            max_step_attempts=self.limits.max_step_attempts,
            max_same_capability_calls=self.limits.max_same_capability_calls,
        )

    def _planner_max_calls(self) -> int:
        return 2 if getattr(self.planner, "name", "") == "llm" else 1

    def _planner_is_model(self) -> bool:
        """True when a planning decision really spends a model call.

        The deterministic planner and the test doubles are pure functions.
        Charging them against ``max_model_calls`` made an 8-step Plan
        impossible, because the budget equals the maximum step count.
        """

        return getattr(self.planner, "name", "") == "llm"

    def _planner_calls_for_budget(self) -> int:
        return self._planner_max_calls() if self._planner_is_model() else 0

    def _planner_calls_actually_spent(self) -> int:
        if not self._planner_is_model():
            return 0
        return max(int(getattr(self.planner, "last_call_count", 1)), 1)

    def _can_spend_model_calls(self, task_id: str, count: int) -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        return task.model_call_count + count <= self.limits.max_model_calls

    def _reserve_model_calls(self, task_id: str, count: int) -> bool:
        task = self.store.get_task(task_id)
        if task is None or task.model_call_count + count > self.limits.max_model_calls:
            return False
        task.model_call_count += count
        task.updated_at = utcnow()
        self.store.commit(task)
        return True

    # -- decision and replanning -----------------------------------------

    def _decide_and_apply(
        self,
        task: TaskRecord,
        plan: Plan,
        observations,
        understanding: TaskUnderstanding | None,
        executed: list[str],
        *,
        no_pending_step: bool = False,
    ) -> TaskRunResult | None:
        planner_calls = self._planner_calls_for_budget()
        if no_pending_step and plan.steps and not self._planner_is_model():
            # A deterministic planner has nothing to decide here: being asked to
            # "continue" with no pending Step used to fail the Task instead of
            # finishing it. A Plan with no steps still goes to the planner, which
            # owns the unsupported / not-a-task verdict.
            return self._complete(
                task,
                plan,
                self.store.list_steps(plan.plan_id),
                executed,
                warnings=list(plan.warnings),
            )
        if not self._can_spend_model_calls(task.task_id, planner_calls):
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "model call budget exceeded",
                },
                executed=executed,
                plan=plan,
            )
        set_capabilities = getattr(self.planner, "set_capabilities", None)
        if callable(set_capabilities):
            set_capabilities(self.registry.list_descriptors())
        try:
            decision = self._planner_call(
                lambda: self.planner.decide_after_observation(
                    task.to_envelope(),
                    plan,
                    observations,
                    self._planning_constraints(),
                    understanding,
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize planner failures
            steps = self.store.list_steps(plan.plan_id)
            finished = bool(steps) and all(
                step.status in {"succeeded", "skipped"} for step in steps
            )
            if finished:
                # Everything the plan asked for is done. Whatever went wrong on
                # the closing "what now?" call -- a dropped connection, an
                # unparseable answer -- it must not throw finished work away.
                # Observed in production as a completed answer lost to one
                # transient "remote HTTP request failed".
                return self._complete(
                    task,
                    plan,
                    steps,
                    executed,
                    warnings=[
                        "planner unavailable at the end of the plan "
                        f"({type(exc).__name__}): {exc}"
                    ],
                )
            return self._fail(
                task,
                {
                    "classification": classify_error(exc),
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                executed=executed,
                plan=plan,
            )
        self._reserve_model_calls(
            task.task_id,
            self._planner_calls_actually_spent(),
        )

        self._append_event(
            self.store.get_task(task.task_id),
            EVENT_PLANNER_DECISION,
            {
                "action": decision.action,
                "reason": decision.reason,
                "required_input": decision.required_input,
                "unsupported_intents": decision.unsupported_intents,
                "warnings": decision.warnings,
                "requires_user_confirmation": decision.requires_user_confirmation,
            },
        )

        if decision.action == "continue":
            steps = self.store.list_steps(plan.plan_id)
            if next_pending_step(steps) is None:
                # The model said "keep going" with nothing left to run. Failing
                # here threw away work the plan had already completed; when every
                # step really did finish, "done" is the honest reading and the
                # wrong action word is only worth a warning.
                unfinished = [
                    step
                    for step in steps
                    if step.status not in {"succeeded", "skipped"}
                ]
                if unfinished:
                    return self._fail(
                        task,
                        {
                            "classification": "permanent_error",
                            "message": "planner returned continue with no pending step",
                        },
                        executed=executed,
                        plan=plan,
                    )
                return self._complete(
                    task,
                    plan,
                    steps,
                    executed,
                    warnings=[
                        "planner returned continue with no pending step; completed instead"
                    ],
                )
            return None
        if decision.action == "replan":
            latest = observations[-1] if observations else None
            if latest is not None and latest.status == "unknown":
                # An unknown external result forbids re-running the call: the
                # effect may already have happened. The Planner may query the
                # external state or give up, but it must not plan the same write
                # again -- re-issuing it is how one submission becomes two.
                return self._fail(
                    task,
                    {
                        "classification": "unknown_external_result",
                        "message": decision.reason
                        or "unknown external result requires manual handling",
                    },
                    executed=executed,
                    plan=plan,
                    terminal_status="unknown",
                )
            return self._activate_replan(
                task, plan, decision, observations, executed
            )
        if decision.action == "request_input":
            output = {
                "requires_user_input": True,
                "missing_information": list(decision.required_input),
                "reason": decision.reason,
            }
            return self._pause_for_input(task, plan, None, output, executed)
        if decision.action == "unsupported" and decision.requires_user_confirmation:
            output = {
                "requires_user_input": True,
                "missing_information": ["partial_execution_confirmation"],
                "unsupported_intents": decision.unsupported_intents,
                "reason": decision.reason,
            }
            return self._pause_for_input(task, plan, None, output, executed)
        if decision.action == "complete":
            return self._complete(
                task,
                plan,
                self.store.list_steps(plan.plan_id),
                executed,
                warnings=_merge_warnings(decision.warnings, plan.warnings),
            )
        if decision.action == "unsupported":
            return self._complete(
                task,
                plan,
                self.store.list_steps(plan.plan_id),
                executed,
                warnings=_merge_warnings(
                    decision.warnings or plan.warnings,
                    [
                        f"unsupported intent: {item}"
                        for item in decision.unsupported_intents
                    ],
                ),
            )
        latest = observations[-1] if observations else None
        unknown_terminal = latest is not None and latest.status == "unknown"
        return self._fail(
            task,
            {
                "classification": (
                    "unknown_external_result"
                    if unknown_terminal
                    else "permanent_error"
                ),
                "message": decision.reason or "planner requested failure",
            },
            executed=executed,
            plan=plan,
            terminal_status="unknown" if unknown_terminal else "failed",
        )

    def _activate_replan(
        self,
        task: TaskRecord,
        old_plan: Plan,
        decision: PlannerDecision,
        observations,
        executed: list[str],
    ) -> TaskRunResult | None:
        new_plan = decision.plan
        if new_plan is None:
            # "replan" with nothing to plan is how a model phrases "there is no
            # way forward". That is not a malformed answer to punish with a
            # failure: the Task closes with the Planner's own words as a
            # warning, so the owner reads a reason instead of a crash.
            return self._complete(
                task,
                old_plan,
                self.store.list_steps(old_plan.plan_id),
                executed,
                warnings=[
                    decision.reason
                    or "planner reported that no plan can make progress"
                ],
            )
        if task.replan_count >= self.limits.max_replans:
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "replan limit exceeded",
                    # Keep the model's own words: "limit exceeded" alone tells
                    # the owner nothing about why the Task stopped.
                    "planner_reason": decision.reason,
                },
                executed=executed,
                plan=old_plan,
            )
        completed_ids = {
            step.step_id
            for step in self.store.list_steps(old_plan.plan_id)
            if step.status == "succeeded"
        }
        if completed_ids.intersection(step.step_id for step in new_plan.steps):
            return self._fail(
                task,
                {
                    "classification": "validation_error",
                    "message": "new plan cannot reuse completed step ids",
                },
                executed=executed,
                plan=old_plan,
            )
        repeat = self._repeated_call(old_plan, new_plan)
        if repeat is not None:
            # Step ids are regenerated on every plan, so the id check above
            # cannot see a re-plan that re-issues the same call under a new id.
            # A deterministic planner would never produce this; a model can.
            return self._fail(
                task,
                {
                    "classification": "validation_error",
                    "message": f"new plan repeats an executed call: {repeat}",
                },
                executed=executed,
                plan=old_plan,
            )
        try:
            new_plan = bind_plan_references(
                new_plan, self.registry.list_descriptors()
            )
            self.validator.validate(new_plan, task.to_envelope())
        except ContractValidationError as exc:
            return self._fail(
                task,
                {
                    "classification": "validation_error",
                    "message": str(exc),
                    "errors": exc.errors,
                },
                executed=executed,
                plan=old_plan,
            )

        new_plan.version = self.store.next_plan_version(task.task_id)
        new_plan.parent_plan_id = new_plan.parent_plan_id or old_plan.plan_id
        new_plan.triggered_by_observation_id = (
            new_plan.triggered_by_observation_id
            or (observations[-1].observation_id if observations else None)
        )
        new_plan.replan_reason = new_plan.replan_reason or decision.reason
        new_plan.status = "validated"
        if old_plan.status in {"draft", "validated", "running"}:
            ensure_plan_transition(old_plan.status, "cancelled")
            old_plan.status = "cancelled"
            self.store.save_plan(old_plan)
        new_by_order = {step.order: step for step in new_plan.steps}
        for old_step in self.store.list_steps(old_plan.plan_id):
            replacement = new_by_order.get(old_step.order)
            if old_step.status == "succeeded" or replacement is None:
                continue
            old_step.replaced_by_step_id = replacement.step_id
            self.store.update_step(old_step)
            # Attempts belong to the external effect, not to the row: a retry
            # expressed as a new Plan version must not reset the step budget,
            # otherwise a failing Step can be re-planned forever.
            replacement.attempt_count = max(
                replacement.attempt_count, old_step.attempt_count
            )
            if replacement.status == "pending" and replacement.attempt_count:
                replacement.status = "ready"
        self.store.invalidate_plans(task.task_id)
        self.store.save_plan(new_plan)
        self.store.save_steps(task.task_id, new_plan.steps)

        task = self.store.get_task(task.task_id)
        task.current_plan_id = new_plan.plan_id
        task.current_plan_version = new_plan.version
        task.objective = new_plan.objective
        task.replan_count += 1
        ensure_task_transition(task.status, "ready")
        task.status = "ready"
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_PLAN_REPLANNED,
                    {
                        "parent_plan_id": old_plan.plan_id,
                        "plan_id": new_plan.plan_id,
                        "version": new_plan.version,
                        "triggered_by_observation_id": new_plan.triggered_by_observation_id,
                        "reason": new_plan.replan_reason,
                    },
                )
            ],
        )
        return None

    def _repeated_call(self, old_plan: Plan, new_plan: Plan) -> str | None:
        """The first Step of ``new_plan`` that re-issues an already-run call.

        A re-plan mints fresh step ids, so the completed-id check above cannot
        see this: the same Capability with the same arguments under a new id is
        a new call as far as that check is concerned, yet it is the same
        external effect and so the same expected failure. A deterministic
        planner cannot produce one; a model asked to re-plan a failure can.

        The arguments are read back from the persisted Call rather than from the
        Step, because the Step holds the plan-time text (it may still contain a
        reference) while the Call holds what was actually sent. `$`-references
        are skipped: as text they say nothing about whether a call repeats.
        """

        executed_calls: set[tuple[str, str]] = set()
        for step in old_plan.steps:
            record = self.store.find_capability_call_for_step(
                old_plan.task_id, step.step_id
            )
            if record is None:
                continue
            # A retryable failure is what a re-plan exists to recover from: the
            # same call, tried again, is the correct move. Every other outcome
            # makes a re-issue either fruitless (permanent failure), a duplicate
            # side effect (success), or unsafe (unknown).
            if record.status == "failed" and (
                record.error or {}
            ).get("classification") != "permanent_error":
                continue
            if has_references(record.arguments):
                continue
            executed_calls.add(self._call_signature(step.capability, record.arguments))
        if not executed_calls:
            return None
        for step in new_plan.steps:
            if has_references(step.arguments):
                continue
            if self._call_signature(step.capability, step.arguments) in executed_calls:
                return f"{step.capability}({_describe_arguments(step.arguments)})"
        return None

    @staticmethod
    def _call_signature(capability: str, arguments) -> tuple[str, str]:
        """Canonical identity of a call, independent of the step that issued it."""

        return (
            capability,
            json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False),
        )

    @staticmethod
    def _repeated_capability_run(step: PlanStep, observations) -> int:
        """Length of the unbroken run of this capability at the tail.

        Only consecutive calls count: a capability that was used earlier in the
        Task, with something else in between, is not part of a loop.
        """

        run = 0
        for item in reversed(observations):
            if item.capability != step.capability:
                break
            run += 1
        return run

    # -- execution --------------------------------------------------------

    def _recover_running_step(
        self,
        task: TaskRecord,
        plan: Plan,
        step: PlanStep,
        executed: list[str],
    ) -> TaskRunResult | None:
        """Resolve a Step whose worker died after marking it ``running``.

        The persisted CapabilityCall, if any, is the only trustworthy record:
        the external call may already have happened, so the Step is settled
        from it instead of being re-issued. When no call row exists the worker
        died before the call and the Step is safe to run.
        """

        existing = self.store.find_capability_call_for_step(task.task_id, step.step_id)
        if existing is None:
            previous = step.status
            step.status = "ready"
            self.store.update_step(step)
            self.log_recovery(task, step, previous, "no capability call recorded; re-running")
            return None
        call = existing
        steps = self.store.list_steps(plan.plan_id)
        target = "succeeded" if call.status == "succeeded" else call.status
        if target not in {"succeeded", "failed", "unknown"}:
            step.status = "ready"
            self.store.update_step(step)
            self.log_recovery(task, step, "running", "call row is not finished; re-running")
            return None
        if call.status == "succeeded" and (call.result or {}).get("requires_user_input"):
            # The capability paused before writing anything, so the Step is
            # still unfinished and must be offered again once input arrives.
            step.status = "ready"
            self.store.update_step(step)
            self.log_recovery(
                task, step, "running", "call paused for user input; re-running"
            )
            return None
        observation = self.executor.to_observation(call)
        self.store.save_observation(observation)
        if call.status == "succeeded":
            step.status = "succeeded"
            self.store.update_step(step)
            if step.step_id not in executed:
                executed.append(step.step_id)
            recovered_task = self.store.get_task(task.task_id)
            recovered_task.checkpoint = build_checkpoint(recovered_task, plan, steps).model_dump(mode="json")
            ensure_task_transition(recovered_task.status, "ready")
            recovered_task.status = "ready"
            recovered_task.updated_at = utcnow()
            self.store.commit(
                recovered_task,
                events=[
                    new_task_event(
                        recovered_task.task_id,
                        EVENT_STEP_SUCCEEDED,
                        {
                            "step_id": step.step_id,
                            "capability": step.capability,
                            "observation_id": observation.observation_id,
                            "recovered": True,
                        },
                    )
                ],
            )
        else:
            error = call.error or {
                "classification": "unknown_external_result"
                if call.status == "unknown"
                else "permanent_error",
                "message": "recovered from an interrupted capability call",
            }
            step.status = target
            self.store.update_step(step, last_error=error)
            recovered_task = self.store.get_task(task.task_id)
            recovered_task.checkpoint = build_checkpoint(recovered_task, plan, steps).model_dump(mode="json")
            ensure_task_transition(recovered_task.status, "ready")
            recovered_task.status = "ready"
            recovered_task.updated_at = utcnow()
            self.store.commit(
                recovered_task,
                events=[
                    new_task_event(
                        recovered_task.task_id,
                        EVENT_STEP_FAILED,
                        {
                            "step_id": step.step_id,
                            "capability": step.capability,
                            "observation_id": observation.observation_id,
                            "error": error,
                            "recovered": True,
                        },
                    )
                ],
            )
        return self._decide_and_apply(
            self.store.get_task(task.task_id),
            self.store.get_plan(plan.plan_id) or plan,
            self.store.list_observations(task.task_id),
            self._load_understanding(self.store.get_task(task.task_id)),
            executed,
        )

    @staticmethod
    def log_recovery(task: TaskRecord, step: PlanStep, previous: str, reason: str) -> None:
        logger.info(
            "recovering step %s of task %s from %s: %s",
            step.step_id,
            task.task_id,
            previous,
            reason,
        )

    def _preflight(
        self,
        task: TaskRecord,
        plan: Plan,
        step: PlanStep,
        capability: CapabilityProtocol,
        understanding: TaskUnderstanding | None,
        executed: list[str],
    ) -> TaskRunResult | None:
        preflight = getattr(capability, "preflight", None)
        if not callable(preflight):
            return None
        try:
            output = preflight(step.arguments)
        except Exception as exc:  # noqa: BLE001 - normalize capability failure
            return self._fail(
                task,
                {
                    "classification": classify_error(exc),
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                executed=executed,
                plan=plan,
                steps=self.store.list_steps(plan.plan_id),
                step_event=True,
                step_id=step.step_id,
            )
        if not isinstance(output, dict) or not output.get("requires_user_input"):
            return None
        observation = self.executor.preflight_observation(task, plan, step, output)
        self.store.save_observation(observation)
        refreshed_plan = self.store.get_plan(plan.plan_id) or plan
        return self._decide_and_apply(
            task,
            refreshed_plan,
            self.store.list_observations(task.task_id),
            understanding,
            executed,
        )

    def _execute_step(
        self,
        task: TaskRecord,
        plan: Plan,
        step: PlanStep,
        understanding: TaskUnderstanding | None,
        executed: list[str],
    ) -> TaskRunResult | None:
        attempt = step.attempt_count + 1
        if attempt > self.limits.max_step_attempts:
            return self._fail(
                task,
                {
                    "classification": "permanent_error",
                    "message": "step attempt limit exceeded",
                },
                executed=executed,
                plan=plan,
                steps=self.store.list_steps(plan.plan_id),
            )
        if step.status == "pending":
            self._mark_step(step, "ready")
        ensure_step_transition(step.status, "running")
        step.status = "running"
        self.store.update_step(step, attempt_count=attempt)
        # Keep the in-memory object truthful: the store persists the attempt
        # count, but a later update_step(step, ...) writes whatever the object
        # still holds. Leaving it stale would reset the budget to zero on every
        # retry, and an in-place retry would then never run out of attempts.
        step.attempt_count = attempt

        task = self.store.get_task(task.task_id)
        ensure_task_transition(task.status, "executing")
        task.status = "executing"
        task.step_count += 1
        task.updated_at = utcnow()
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_STEP_STARTED,
                    {
                        "step_id": step.step_id,
                        "capability": step.capability,
                        "attempt": attempt,
                    },
                )
            ],
        )

        call = self.executor.execute(task, plan, step, attempt)
        observation = self.executor.to_observation(call)
        # Only a fresh execution reports provenance and spend: a call row that
        # was reused unchanged already paid for itself the first time.
        spent_model_calls = 0
        if call.status == "succeeded" and call.attempt == attempt:
            spent_model_calls = self._capture_evidence(observation)
        self.store.save_observation(observation)
        if spent_model_calls:
            self._charge_model_calls(task.task_id, spent_model_calls)

        if call.status == "succeeded":
            output = call.result or {}
            if output.get("requires_user_input"):
                if step.status == "running":
                    step.status = "ready"
                    self.store.update_step(step)
            else:
                step.status = "succeeded"
                self.store.update_step(step)
                executed.append(step.step_id)
            task = self.store.get_task(task.task_id)
            steps = self.store.list_steps(plan.plan_id)
            task.checkpoint = build_checkpoint(task, plan, steps).model_dump(mode="json")
            ensure_task_transition(task.status, "ready")
            task.status = "ready"
            task.updated_at = utcnow()
            events = [
                new_task_event(
                    task.task_id,
                    EVENT_STEP_SUCCEEDED,
                    {
                        "step_id": step.step_id,
                        "capability": step.capability,
                        "observation_id": observation.observation_id,
                        "requires_user_input": bool(output.get("requires_user_input")),
                    },
                )
            ]
            # The draft a client shows and the to-do row have different
            # lifetimes. This is the moment the row starts existing, so it is
            # also the moment to tell the client the preview became real.
            preview = self._preview_event(task)
            if preview is not None:
                events.append(
                    new_task_event(task.task_id, EVENT_PREVIEW_CONFIRMED, preview)
                )
            self.store.commit(task, events=events)
        else:
            error = call.error or {
                "classification": "permanent_error",
                "message": "capability failed",
            }
            # Same path, walked again. A transient failure does not invalidate
            # the Plan, so it must not cost a model call or a new Plan version.
            # The retry reuses the CapabilityCall row and with it the same
            # request_id, so an external system still deduplicates the call.
            if (
                error.get("classification") == "retryable_error"
                and attempt < self.limits.max_step_attempts
            ):
                self._mark_step(step, "ready")
                time.sleep(self.limits.backoff_for(attempt))
                return None
            target_status = (
                "unknown"
                if error.get("classification") == "unknown_external_result"
                else "failed"
            )
            if step.status != target_status:
                ensure_step_transition(step.status, target_status)
                step.status = target_status
            self.store.update_step(step, last_error=error)
            task = self.store.get_task(task.task_id)
            steps = self.store.list_steps(plan.plan_id)
            task.checkpoint = build_checkpoint(task, plan, steps).model_dump(mode="json")
            ensure_task_transition(task.status, "ready")
            task.status = "ready"
            task.updated_at = utcnow()
            self.store.commit(
                task,
                events=[
                    new_task_event(
                        task.task_id,
                        EVENT_STEP_FAILED,
                        {
                            "step_id": step.step_id,
                            "capability": step.capability,
                            "observation_id": observation.observation_id,
                            "error": error,
                        },
                    )
                ],
            )

        refreshed_plan = self.store.get_plan(plan.plan_id) or plan
        return self._decide_and_apply(
            self.store.get_task(task.task_id),
            refreshed_plan,
            self.store.list_observations(task.task_id),
            understanding,
            executed,
        )

    # -- waits and terminal states ---------------------------------------

    def _pause_for_input(
        self,
        task: TaskRecord,
        plan: Plan,
        step: PlanStep | None,
        output: dict[str, Any],
        executed: list[str],
    ) -> TaskRunResult:
        if step is not None and step.status in {"pending", "failed"}:
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
                        "plan_id": plan.plan_id,
                        "step_id": step.step_id if step else None,
                        "missing_information": list(
                            output.get("missing_information") or []
                        ),
                        "unsupported_intents": list(
                            output.get("unsupported_intents") or []
                        ),
                    },
                )
            ],
        )
        return self._result(task, executed=executed, waiting_for="waiting_input")

    def _wait_for_approval(
        self,
        task: TaskRecord,
        plan: Plan,
        step: PlanStep,
        reason: str | None,
        executed: list[str],
    ) -> TaskRunResult:
        if self.approval_gateway is None:
            return self._fail(
                task,
                {
                    "classification": "policy_denied",
                    "message": "approval required but no approval gateway configured",
                },
                executed=executed,
                plan=plan,
            )
        approval = self.approval_gateway.request(
            task,
            step,
            reason=reason,
            version=step.attempt_count + 1,
        )
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
        return self._result(task, executed=executed, waiting_for="waiting_approval")

    def resume_after_approval(
        self, task_id: str, *, lease_owner: str = "worker"
    ) -> TaskRunResult:
        return self.run_task(task_id, lease_owner=lease_owner)

    def _complete(
        self,
        task: TaskRecord,
        plan: Plan,
        steps: list[PlanStep],
        executed: list[str],
        *,
        warnings: list[str] | None = None,
    ) -> TaskRunResult:
        # A Plan is only complete when no Step still needs work. Letting a
        # Planner stop here silently dropped pending Steps of a side effecting
        # Plan and reported success.
        unfinished = [
            step.step_id
            for step in steps
            if step.status in {"pending", "ready", "running", "waiting_approval"}
        ]
        if unfinished:
            blocking = [
                step
                for step in steps
                if step.step_id in set(unfinished)
                and self._step_blocks_completion(step)
            ]
            if blocking:
                return self._fail(
                    task,
                    {
                        "classification": "permanent_error",
                        "message": "plan is not finished",
                        "unfinished_steps": [step.step_id for step in blocking],
                    },
                    executed=executed,
                    plan=plan,
                    steps=steps,
                )
            # The paperwork is the only thing left: the Planner decided these
            # read-only Steps are no longer needed, so they are closed out
            # explicitly instead of being left pending forever.
            for step in steps:
                if step.step_id in set(unfinished):
                    step.status = "skipped"
                    self.store.update_step(step)
            warnings = list(warnings or [])
            warnings.extend(
                f"skipped unfinished step: {step_id}" for step_id in unfinished
            )
        if plan.status in {"draft", "validated", "running"}:
            ensure_plan_transition(plan.status, "completed")
            plan.status = "completed"
            self.store.save_plan(plan)
        task = self.store.get_task(task.task_id)
        ensure_task_transition(task.status, "succeeded")
        task.status = "succeeded"
        result: dict = {"warnings": list(warnings or [])}
        answer = self._answer_payload(task)
        if answer is not None:
            result["answer"] = answer["answer"]
            result["citations"] = answer["citations"]
        task.result = result
        task.checkpoint = build_checkpoint(task, plan, steps).model_dump(mode="json")
        task.updated_at = utcnow()
        payload: dict = {
            "plan_id": plan.plan_id,
            "executed_steps": executed,
            "warnings": list(warnings or []),
        }
        if answer is not None:
            payload["answer"] = answer["answer"]
            payload["citations"] = answer["citations"]
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task.task_id,
                    EVENT_TASK_COMPLETED,
                    payload,
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
        plan: Plan | None = None,
        steps: list[PlanStep] | None = None,
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
        events: list = []
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

    def _mark_step(
        self, step: PlanStep, status: str, *, last_error: dict | None = None
    ) -> None:
        if step.status != status:
            ensure_step_transition(step.status, status)
            step.status = status
        self.store.update_step(step, last_error=last_error)

    def _append_event(self, task: TaskRecord, event_type: str, payload: dict) -> None:
        self.store.commit(
            task,
            events=[new_task_event(task.task_id, event_type, payload)],
        )

    def _approved_approval(self, task: TaskRecord, step: PlanStep):
        approvals = [
            item
            for item in self.store.list_approvals(task_id=task.task_id)
            if item.step_id == step.step_id
            and item.status == "approved"
            # An approved id is only usable while the Step still carries the
            # arguments the user approved; a re-plan may rewrite them.
            and approval_matches_arguments(item, step.arguments)
        ]
        if not approvals:
            return None
        approvals.sort(key=lambda item: item.version)
        return approvals[-1]

    def _step_blocks_completion(self, step: PlanStep) -> bool:
        """Whether closing the Task now would drop real, unfinished work.

        A read-only Step can be closed out by a Planner that changed its mind.
        A write Step, a Step whose external effect is unconfirmed, or one that is
        mid-flight must not be: those are the cases where "succeeded" would be a
        lie.
        """

        credential = self.registry.find(step.capability)
        descriptor = getattr(credential, "descriptor", None)
        if step.status in {"running", "waiting_approval"}:
            return True
        if descriptor is None:
            return True
        if descriptor.side_effect or descriptor.requires_approval:
            return True
        return not descriptor.idempotent

    def _result(
        self,
        task: TaskRecord,
        *,
        executed: list[str] | None = None,
        waiting_for: str | None = None,
    ) -> TaskRunResult:
        warnings = []
        if isinstance(task.result, dict):
            warnings = [str(item) for item in task.result.get("warnings", [])]
        return TaskRunResult(
            task_id=task.task_id,
            status=task.status,
            plan_id=task.current_plan_id,
            executed_step_ids=list(executed or []),
            waiting_for=waiting_for,
            warnings=warnings,
        )

    def _preprocess_attachments(self, task: TaskRecord) -> None:
        """附件预处理：在Understanding之前增强上下文"""
        from app.kernel.attachment_enricher import AttachmentContextEnricher
        from app.infrastructure.attachment_store import RedisAttachmentStore
        import redis

        try:
            # 获取settings和attachment_store
            settings = getattr(self, 'settings', None)
            if not settings:
                # 尝试从store获取settings
                settings = getattr(self.store, 'settings', None)

            if not settings:
                logger.warning("无法获取settings，跳过附件预处理")
                return

            r = redis.from_url(settings.redis_url)
            attachment_store = RedisAttachmentStore(r, settings.attachment_ttl_hours)

            enricher = AttachmentContextEnricher(
                rag_service_url=settings.rag_service_url,
                rag_internal_token=settings.rag_internal_token,
                attachment_store=attachment_store
            )

            enriched = enricher.enrich(task.input)
            # 更新text为增强后的版本
            task.input["text"] = enriched.enriched_text
            # 保留原始text供日志使用
            task.input["_original_text"] = enriched.original_text
            # 只有用户明确引用附件时，规划器才从附件参数里取材
            task.input["_attachment_referenced"] = enriched.attachment_referenced
            task.input["_attachment_excerpt"] = enriched.attachment_excerpt
            task.input["_attachment_file_names"] = [
                str(item.get("file_name"))
                for item in enriched.attachment_metadata
                if item.get("file_name")
            ]
            # 标记已处理
            task.input["_attachment_processed"] = True

            # AgentStore 没有 save_task；commit 的 upsert 会一并写回 input，
            # 这是把增强文本带给后续 Understanding/Planning 的唯一通道。
            self.store.commit(task)

            logger.info(f"附件预处理成功，增强文本长度: {len(enriched.enriched_text)}")
        except Exception as e:
            logger.exception(f"附件预处理失败: {str(e)}")
            # 失败不中断任务，使用原始text继续


def _merge_warnings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in merged:
                merged.append(item)
    return merged
