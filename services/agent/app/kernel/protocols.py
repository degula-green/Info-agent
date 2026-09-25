from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from app.kernel.models import (
    ApprovalRecord,
    CapabilityCallRecord,
    CapabilityDescriptor,
    EvidenceRecord,
    OutboxEvent,
    Plan,
    PlanStep,
    PolicyDecision,
    Observation,
    TaskEvent,
    TaskEnvelope,
    TaskInput,
    TaskRecord,
    TaskUnderstanding,
)


class TaskUnderstandingProvider(Protocol):
    def understand(self, task: TaskEnvelope) -> TaskUnderstanding:
        ...


class Planner(Protocol):
    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
    ) -> Plan:
        ...


class Capability(Protocol):
    descriptor: CapabilityDescriptor

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        ...

    def execute(self, arguments: BaseModel) -> dict[str, Any]:
        ...


class PolicyEngine(Protocol):
    def evaluate(
        self,
        task: TaskEnvelope,
        step: PlanStep,
        capability: CapabilityDescriptor,
    ) -> PolicyDecision:
        ...


class ObservationStore(Protocol):
    def save(self, observation: Observation) -> None:
        ...

    def list_for_task(self, task_id: str) -> list[Observation]:
        ...

    def save_task(self, task: TaskEnvelope) -> None:
        ...

    def get_task(self, task_id: str) -> TaskEnvelope | None:
        ...

    def save_plan(self, plan: Plan) -> None:
        ...

    def get_plan(self, plan_id: str) -> Plan | None:
        ...


class AgentStore(Protocol):
    """Authoritative Task/Plan/Step/Event/Outbox storage.

    Implemented by the in-memory store (tests) and the PostgreSQL store
    (production). ``commit`` persists a Task transition, its Task Events and
    its Outbox Events in a single atomic unit of work.
    """

    def create_task(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent],
        outbox_events: list[OutboxEvent],
    ) -> TaskRecord:
        ...

    def get_task(self, task_id: str) -> TaskRecord | None:
        ...

    def find_task_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        ...

    def commit(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent] | None = None,
        outbox_events: list[OutboxEvent] | None = None,
    ) -> None:
        ...

    def list_unfinished_tasks(self, limit: int = 50) -> list[TaskRecord]:
        ...

    def acquire_lease(self, task_id: str, owner: str, seconds: float) -> bool:
        ...

    def release_lease(self, task_id: str, owner: str) -> None:
        ...

    def add_input(self, item: TaskInput) -> None:
        ...

    def list_inputs(self, task_id: str) -> list[TaskInput]:
        ...

    def save_plan(self, plan: Plan) -> None:
        ...

    def get_plan(self, plan_id: str) -> Plan | None:
        ...

    def get_active_plan(self, task_id: str) -> Plan | None:
        ...

    def invalidate_plans(self, task_id: str, *, except_plan_id: str | None = None) -> None:
        ...

    def save_steps(self, task_id: str, steps: list[PlanStep]) -> None:
        ...

    def update_step(
        self,
        step: PlanStep,
        *,
        attempt_count: int | None = None,
        approved_version: int | None = None,
        last_error: dict[str, Any] | None = None,
    ) -> None:
        ...

    def list_steps(self, plan_id: str) -> list[PlanStep]:
        ...

    def get_step(self, step_id: str) -> PlanStep | None:
        ...

    def save_observation(self, observation: Observation) -> None:
        ...

    def list_observations(self, task_id: str) -> list[Observation]:
        ...

    def get_capability_call(self, idempotency_key: str) -> CapabilityCallRecord | None:
        ...

    def save_capability_call(self, call: CapabilityCallRecord) -> None:
        ...

    def save_evidence(self, evidence: EvidenceRecord) -> None:
        ...

    def save_approval(self, approval: ApprovalRecord) -> None:
        ...

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        ...

    def list_approvals(
        self, *, task_id: str | None = None, owner_user_id: str | None = None
    ) -> list[ApprovalRecord]:
        ...

    def append_event(self, event: TaskEvent) -> TaskEvent:
        ...

    def list_events(self, task_id: str, *, after_sequence: int = 0) -> list[TaskEvent]:
        ...

    def enqueue_outbox(self, event: OutboxEvent) -> None:
        ...

    def pending_outbox(self, limit: int = 50) -> list[OutboxEvent]:
        ...

    def mark_outbox_sent(self, event_id: str) -> None:
        ...

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        ...


class TaskEventPublisher(Protocol):
    """Delivery of wake-up signals; Redis in production, fake in tests."""

    def publish(self, event: OutboxEvent) -> None:
        ...
