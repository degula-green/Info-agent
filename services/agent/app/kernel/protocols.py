from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from app.kernel.models import (
    ApprovalRecord,
    CapabilityCallRecord,
    CapabilityDescriptor,
    ConversationRecord,
    EvidenceRecord,
    MessageRecord,
    OutboxEvent,
    Plan,
    PlanStep,
    PlannerDecision,
    PlanningConstraints,
    PolicyDecision,
    Observation,
    TaskEvent,
    TaskEnvelope,
    TaskInput,
    TaskRecord,
    TaskUnderstanding,
    TodoRecord,
)


class TodoStore(Protocol):
    """Authoritative ledger for ``todo.create``.

    Kept separate from :class:`AgentStore` on purpose: the execution kernel never
    reads a to-do, and the desktop surface never reads a Plan. PostgreSQL is the
    production implementation; the in-memory one backs tests and local runs.
    """

    def create_todo(self, todo: TodoRecord) -> TodoRecord:
        ...

    def get_todo(self, todo_id: str) -> TodoRecord | None:
        ...

    def find_todo_by_idempotency_key(self, idempotency_key: str) -> TodoRecord | None:
        """Lets a retried Step reuse the row it already wrote."""
        ...

    def list_todos(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[TodoRecord]:
        """Read-only listing for the desktop; the runtime never calls this."""
        ...

    def update_todo(
        self,
        todo_id: str,
        *,
        owner_user_id: str,
        changes: dict[str, Any],
    ) -> TodoRecord | None:
        """Partial update from the desktop (title, due time, status)."""
        ...

    def delete_todo(self, todo_id: str, *, owner_user_id: str) -> bool:
        ...


class TaskUnderstandingProvider(Protocol):
    def understand(
        self,
        task: TaskEnvelope,
        *,
        min_confidence: float | None = None,
    ) -> TaskUnderstanding:
        ...


class Planner(Protocol):
    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> Plan:
        ...

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
    ) -> PlannerDecision:
        ...


class Capability(Protocol):
    descriptor: CapabilityDescriptor

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        ...

    def execute(self, arguments: BaseModel) -> dict[str, Any]:
        ...


class PreflightCapability(Protocol):
    """Optional capability extension: report missing input before approval.

    The kernel only calls it when a registered capability implements it. The
    return value uses the same shape as an execution result
    (``{"requires_user_input": True, "missing_information": [...]}``); ``None``
    or a result without ``requires_user_input`` means the capability is ready.
    """

    def preflight(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        ...


class ReconcilableCapability(Protocol):
    """Optional capability extension: look up what became of an earlier call.

    Only capabilities whose descriptor declares ``reconcilable = True`` need to
    implement it. The kernel calls it when a call's outcome is unknown, to find
    out whether the external effect actually happened instead of re-issuing it
    blindly. Reading is side-effect free, so it may be called repeatedly.
    """

    def reconcile(self, request_id: str) -> dict[str, Any]:
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
        inputs: list[TaskInput] | None = None,
        conversation: ConversationRecord | None = None,
        messages: list[MessageRecord] | None = None,
    ) -> TaskRecord:
        """Persist a new Task and its creation-time side records atomically.

        ``conversation`` and ``messages`` are optional because knowledge-event
        fan-out has no user-facing conversation. When supplied, they are written
        in the same transaction as the Task so an idempotent replay cannot leave
        a half-created chat turn behind.
        """
        ...

    def get_task(self, task_id: str) -> TaskRecord | None:
        ...

    def find_task_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        ...

    def list_tasks_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        """Read-only listing for the API; the runtime never calls this."""
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

    def next_plan_version(self, task_id: str) -> int:
        """Version for a new Plan of this Task: existing maximum + 1.

        ``agent_plans`` is unique on ``(task_id, version)``, so a re-plan after
        user input must never reuse the version of the Plan it replaces.
        """
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

    def find_capability_call_for_step(
        self, task_id: str, step_id: str
    ) -> CapabilityCallRecord | None:
        """Latest persisted call for a Step; used to resume an interrupted Step."""
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

    # -- conversation history ------------------------------------------------

    def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        ...

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        ...

    def save_conversation(self, conversation: ConversationRecord) -> None:
        ...

    def list_conversations_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationRecord]:
        ...

    def count_conversations_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
    ) -> int:
        ...

    def delete_conversation(
        self, conversation_id: str, *, owner_user_id: str
    ) -> bool:
        ...

    def add_message(self, message: MessageRecord) -> MessageRecord:
        ...

    def get_message(self, message_id: str) -> MessageRecord | None:
        ...

    def update_message(self, message: MessageRecord) -> None:
        ...

    def list_messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[MessageRecord]:
        ...

    def count_messages(self, conversation_id: str) -> int:
        ...


class TaskEventPublisher(Protocol):
    """Delivery of wake-up signals; Redis in production, fake in tests."""

    def publish(self, event: OutboxEvent) -> None:
        ...
