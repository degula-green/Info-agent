"""In-memory implementation of the AgentStore protocol.

Used by unit and integration tests so the kernel can be verified without a real
PostgreSQL or Redis instance. Behaviour mirrors the PostgreSQL store.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from app.kernel.events import utcnow
from app.kernel.models import (
    ApprovalRecord,
    CapabilityCallRecord,
    EvidenceRecord,
    Observation,
    OutboxEvent,
    Plan,
    PlanStep,
    TaskEvent,
    TaskInput,
    TaskRecord,
)


class InMemoryAgentStore:
    """Test/dev store. Outbox retries are immediate unless a latency is given."""

    def __init__(self, *, outbox_retry_latency_seconds: float = 0.0) -> None:
        self._lock = threading.RLock()
        self._outbox_retry_latency_seconds = outbox_retry_latency_seconds
        self.tasks: dict[str, TaskRecord] = {}
        self.idempotency: dict[str, str] = {}
        self.inputs: dict[str, list[TaskInput]] = {}
        self.plans: dict[str, Plan] = {}
        self.active_plans: dict[str, str] = {}
        self.steps: dict[str, PlanStep] = {}
        self.observations: dict[str, list[Observation]] = {}
        self.calls: dict[str, CapabilityCallRecord] = {}
        self.evidence: list[EvidenceRecord] = []
        self.approvals: dict[str, ApprovalRecord] = {}
        self.events: dict[str, list[TaskEvent]] = {}
        self.outbox: dict[str, OutboxEvent] = {}

    # -- tasks ------------------------------------------------------------

    def create_task(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent] | None = None,
        outbox_events: list[OutboxEvent] | None = None,
    ) -> TaskRecord:
        with self._lock:
            if task.idempotency_key:
                existing_id = self.idempotency.get(task.idempotency_key)
                if existing_id is not None:
                    return self.tasks[existing_id].model_copy(deep=True)
                self.idempotency[task.idempotency_key] = task.task_id
            self.tasks[task.task_id] = task.model_copy(deep=True)
            self.inputs.setdefault(task.task_id, [])
            self.observations.setdefault(task.task_id, [])
            self.events.setdefault(task.task_id, [])
            for event in events or []:
                self.append_event(event)
            for item in outbox_events or []:
                self.enqueue_outbox(item)
            return self.tasks[task.task_id].model_copy(deep=True)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            task = self.tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def find_task_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        with self._lock:
            task_id = self.idempotency.get(idempotency_key)
            if task_id is None:
                return None
            return self.get_task(task_id)

    def commit(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent] | None = None,
        outbox_events: list[OutboxEvent] | None = None,
    ) -> None:
        with self._lock:
            stored = task.model_copy(deep=True)
            stored.updated_at = utcnow()
            self.tasks[task.task_id] = stored
            self.events.setdefault(task.task_id, [])
            for event in events or []:
                self.append_event(event)
            for item in outbox_events or []:
                self.enqueue_outbox(item)

    def list_unfinished_tasks(self, limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            terminal = {"succeeded", "failed", "cancelled", "unknown"}
            tasks = [task for task in self.tasks.values() if task.status not in terminal]
            tasks.sort(key=lambda item: item.created_at)
            return [task.model_copy(deep=True) for task in tasks[:limit]]

    def acquire_lease(self, task_id: str, owner: str, seconds: float) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return False
            now = utcnow()
            if task.lease_owner and task.lease_expires_at and task.lease_expires_at > now:
                return task.lease_owner == owner
            task.lease_owner = owner
            task.lease_expires_at = now + timedelta(seconds=seconds)
            return True

    def release_lease(self, task_id: str, owner: str) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if task is not None and task.lease_owner == owner:
                task.lease_owner = None
                task.lease_expires_at = None

    # -- inputs -----------------------------------------------------------

    def add_input(self, item: TaskInput) -> None:
        with self._lock:
            bucket = self.inputs.setdefault(item.task_id, [])
            for index, existing in enumerate(bucket):
                if existing.version == item.version:
                    bucket[index] = item.model_copy(deep=True)
                    return
            bucket.append(item.model_copy(deep=True))
            bucket.sort(key=lambda value: value.version)

    def list_inputs(self, task_id: str) -> list[TaskInput]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self.inputs.get(task_id, [])]

    # -- plans and steps --------------------------------------------------

    def save_plan(self, plan: Plan) -> None:
        with self._lock:
            self.plans[plan.plan_id] = plan.model_copy(deep=True)
            self.active_plans[plan.task_id] = plan.plan_id

    def get_plan(self, plan_id: str) -> Plan | None:
        with self._lock:
            plan = self.plans.get(plan_id)
            if plan is None:
                return None
            copy = plan.model_copy(deep=True)
            copy.steps = [self.steps[step.step_id].model_copy(deep=True) for step in copy.steps if step.step_id in self.steps]
            return copy

    def get_active_plan(self, task_id: str) -> Plan | None:
        with self._lock:
            plan_id = self.active_plans.get(task_id)
            return self.get_plan(plan_id) if plan_id else None

    def invalidate_plans(self, task_id: str, *, except_plan_id: str | None = None) -> None:
        with self._lock:
            current = self.active_plans.get(task_id)
            if current and current != except_plan_id:
                self.active_plans.pop(task_id, None)

    def save_steps(self, task_id: str, steps: list[PlanStep]) -> None:
        with self._lock:
            for step in steps:
                self.steps[step.step_id] = step.model_copy(deep=True)
            for plan_id, plan in self.plans.items():
                if plan.task_id == task_id and any(step.plan_id == plan_id for step in steps):
                    plan.steps = [self.steps[s.step_id].model_copy(deep=True) for s in steps]

    def update_step(
        self,
        step: PlanStep,
        *,
        attempt_count: int | None = None,
        approved_version: int | None = None,
        last_error: dict | None = None,
    ) -> None:
        with self._lock:
            stored = step.model_copy(deep=True)
            self.steps[step.step_id] = stored
            plan = self.plans.get(step.plan_id)
            if plan is not None:
                for index, item in enumerate(plan.steps):
                    if item.step_id == step.step_id:
                        plan.steps[index] = stored.model_copy(deep=True)

    def list_steps(self, plan_id: str) -> list[PlanStep]:
        with self._lock:
            plan = self.plans.get(plan_id)
            if plan is None:
                return []
            steps = [self.steps[step.step_id] for step in plan.steps if step.step_id in self.steps]
            steps.sort(key=lambda value: value.order)
            return [step.model_copy(deep=True) for step in steps]

    def get_step(self, step_id: str) -> PlanStep | None:
        with self._lock:
            step = self.steps.get(step_id)
            return step.model_copy(deep=True) if step else None

    # -- observations, calls, evidence ------------------------------------

    def save_observation(self, observation: Observation) -> None:
        with self._lock:
            bucket = self.observations.setdefault(observation.task_id, [])
            for index, existing in enumerate(bucket):
                if existing.observation_id == observation.observation_id:
                    bucket[index] = observation.model_copy(deep=True)
                    return
            bucket.append(observation.model_copy(deep=True))

    def list_observations(self, task_id: str) -> list[Observation]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self.observations.get(task_id, [])]

    def get_capability_call(self, idempotency_key: str) -> CapabilityCallRecord | None:
        with self._lock:
            call = self.calls.get(idempotency_key)
            return call.model_copy(deep=True) if call else None

    def save_capability_call(self, call: CapabilityCallRecord) -> None:
        with self._lock:
            self.calls[call.idempotency_key] = call.model_copy(deep=True)

    def save_evidence(self, evidence: EvidenceRecord) -> None:
        with self._lock:
            self.evidence.append(evidence.model_copy(deep=True))

    # -- approvals --------------------------------------------------------

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self._lock:
            self.approvals[approval.approval_id] = approval.model_copy(deep=True)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            approval = self.approvals.get(approval_id)
            return approval.model_copy(deep=True) if approval else None

    def list_approvals(
        self, *, task_id: str | None = None, owner_user_id: str | None = None
    ) -> list[ApprovalRecord]:
        with self._lock:
            items = list(self.approvals.values())
        if task_id is not None:
            items = [item for item in items if item.task_id == task_id]
        if owner_user_id is not None:
            filtered = []
            for item in items:
                task = self.tasks.get(item.task_id)
                if task is not None and task.owner_user_id == owner_user_id:
                    filtered.append(item)
            items = filtered
        items.sort(key=lambda item: item.created_at)
        return [item.model_copy(deep=True) for item in items]

    # -- events -----------------------------------------------------------

    def append_event(self, event: TaskEvent) -> TaskEvent:
        with self._lock:
            bucket = self.events.setdefault(event.task_id, [])
            stored = event.model_copy(deep=True)
            if stored.sequence <= 0:
                stored.sequence = len(bucket) + 1
            bucket.append(stored)
            return stored.model_copy(deep=True)

    def list_events(self, task_id: str, *, after_sequence: int = 0) -> list[TaskEvent]:
        with self._lock:
            bucket = self.events.get(task_id, [])
            return [
                item.model_copy(deep=True)
                for item in bucket
                if item.sequence > after_sequence
            ]

    # -- outbox -----------------------------------------------------------

    def enqueue_outbox(self, event: OutboxEvent) -> None:
        with self._lock:
            self.outbox[event.event_id] = event.model_copy(deep=True)

    def pending_outbox(self, limit: int = 50) -> list[OutboxEvent]:
        with self._lock:
            now = utcnow()
            items = [
                item
                for item in self.outbox.values()
                if item.status == "pending" and (item.available_at is None or item.available_at <= now)
            ]
            items.sort(key=lambda item: item.created_at)
            return [item.model_copy(deep=True) for item in items[:limit]]

    def mark_outbox_sent(self, event_id: str) -> None:
        with self._lock:
            item = self.outbox.get(event_id)
            if item is None:
                return
            item.status = "sent"
            item.published_at = utcnow()

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        with self._lock:
            item = self.outbox.get(event_id)
            if item is None:
                return
            item.status = "pending"
            item.attempt_count += 1
            item.last_error = error
            item.available_at = utcnow() + timedelta(
                seconds=max(self._outbox_retry_latency_seconds, 0.0)
            )
