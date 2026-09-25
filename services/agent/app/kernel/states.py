"""Task, Plan and Step state machines for the Agent execution kernel."""

from __future__ import annotations

from app.kernel.errors import InvalidStateTransitionError

TASK_STATUSES = (
    "received",
    "planning",
    "ready",
    "executing",
    "waiting_input",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
)

PLAN_STATUSES = ("draft", "validated", "running", "completed", "failed", "cancelled")

STEP_STATUSES = (
    "pending",
    "ready",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "unknown",
    "skipped",
)

CALL_STATUSES = ("pending", "running", "succeeded", "failed", "unknown")

TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "unknown"})

WAITING_TASK_STATUSES = frozenset({"waiting_input", "waiting_approval"})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"planning", "ready", "executing", "waiting_input", "cancelled", "failed"}),
    "planning": frozenset({"ready", "waiting_input", "failed", "cancelled"}),
    "ready": frozenset({"executing", "waiting_approval", "waiting_input", "succeeded", "failed", "cancelled"}),
    "executing": frozenset(
        {"ready", "waiting_approval", "waiting_input", "succeeded", "failed", "cancelled", "unknown"}
    ),
    "waiting_input": frozenset({"planning", "ready", "executing", "cancelled", "failed"}),
    "waiting_approval": frozenset({"ready", "executing", "cancelled", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "unknown": frozenset(),
}

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "skipped", "failed", "waiting_approval"}),
    "ready": frozenset({"running", "waiting_approval", "skipped", "failed"}),
    "running": frozenset({"succeeded", "failed", "unknown", "ready", "waiting_approval"}),
    "waiting_approval": frozenset({"ready", "failed", "skipped"}),
    "succeeded": frozenset(),
    "failed": frozenset({"ready"}),
    "unknown": frozenset(),
    "skipped": frozenset(),
}

PLAN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"validated", "failed", "cancelled"}),
    "validated": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

# Task Event types persisted to PostgreSQL and streamed to clients.
EVENT_TASK_ACCEPTED = "task.accepted"
EVENT_TASK_PLANNING = "task.planning"
EVENT_PLAN_CREATED = "plan.created"
EVENT_STEP_STARTED = "step.started"
EVENT_STEP_SUCCEEDED = "step.succeeded"
EVENT_STEP_FAILED = "step.failed"
EVENT_TASK_RETRYING = "task.retrying"
EVENT_TASK_WAITING_APPROVAL = "task.waiting_approval"
EVENT_TASK_WAITING_INPUT = "task.waiting_input"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TASK_FAILED = "task.failed"
EVENT_TASK_CANCELLED = "task.cancelled"
EVENT_TASK_INPUT_RECEIVED = "task.input_received"
EVENT_APPROVAL_APPROVED = "approval.approved"
EVENT_APPROVAL_REJECTED = "approval.rejected"

KNOWN_EVENT_TYPES = (
    EVENT_TASK_ACCEPTED,
    EVENT_TASK_PLANNING,
    EVENT_PLAN_CREATED,
    EVENT_STEP_STARTED,
    EVENT_STEP_SUCCEEDED,
    EVENT_STEP_FAILED,
    EVENT_TASK_RETRYING,
    EVENT_TASK_WAITING_APPROVAL,
    EVENT_TASK_WAITING_INPUT,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_INPUT_RECEIVED,
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_REJECTED,
)


def ensure_task_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in TASK_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError("task", current, target)


def ensure_step_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in STEP_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError("plan_step", current, target)


def ensure_plan_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in PLAN_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError("plan", current, target)
