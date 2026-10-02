from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskEnvelope(BaseModel):
    task_id: str
    source_type: Literal["chat", "knowledge_event"]
    owner_user_id: str
    input: dict[str, Any]
    source_ref: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class UnderstandingIntent(BaseModel):
    name: str
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: str | None = None


class TaskUnderstanding(BaseModel):
    is_task: bool
    goal: str
    task_kind: Literal["answer", "action", "mixed"] | None = None
    intent_candidates: list[UnderstandingIntent] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None


class PlanStep(BaseModel):
    step_id: str
    plan_id: str
    order: int = Field(ge=1)
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    attempt_count: int = Field(default=0, ge=0)
    replaced_by_step_id: str | None = None


class StepOutputRef(BaseModel):
    """Planner-facing reference to an earlier Step output."""

    step: int = Field(ge=1)
    output: str = Field(min_length=1, max_length=200)


class Plan(BaseModel):
    plan_id: str
    task_id: str
    version: int = Field(default=1, ge=1)
    parent_plan_id: str | None = None
    triggered_by_observation_id: str | None = None
    replan_reason: str | None = None
    unsupported_intents: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_user_confirmation: bool = False
    objective: str
    steps: list[PlanStep] = Field(default_factory=list)
    status: str = "draft"


class CapabilityInputBinding(BaseModel):
    """Maps a planner-facing reference argument to a runtime argument."""

    planner_argument: str
    runtime_argument: str
    source_capability: str
    source_output: str


class CapabilityDescriptor(BaseModel):
    name: str
    description: str
    # The argument and result shape the Planner may plan against, exported from
    # the capability's own Pydantic models. Without them the model has to guess
    # field names, and every guess costs a repair round at execution time.
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # The planner sees *_ref arguments for cross-step dependencies. The
    # runtime still receives the original argument after binding.
    planner_input_schema: dict[str, Any] = Field(default_factory=dict)
    input_bindings: list[CapabilityInputBinding] = Field(default_factory=list)
    # The argument that must carry the user's own words. The Planner never sees
    # it -- the planner code copies the Task text in -- because a value the
    # model may paraphrase cannot be a trust anchor. web.research, for example,
    # decides which URLs it may fetch by looking for them in this argument.
    task_text_argument: str | None = None
    risk_level: str
    side_effect: bool
    requires_approval: bool
    idempotent: bool
    timeout_seconds: int = Field(gt=0)
    # Whether the outcome of a call can be read back after the fact (for
    # example by querying the external system with the ``request_id``). A
    # side-effecting capability that is not reconcilable must ask for approval:
    # once such a call is in flight there is no way to learn whether it landed.
    reconcilable: bool = False


class PolicyDecision(BaseModel):
    action: Literal["allow", "deny", "require_approval"]
    reason: str | None = None


class PlanningConstraints(BaseModel):
    max_steps: int = Field(default=8, ge=1)
    max_replans: int = Field(default=3, ge=0)
    max_model_calls: int = Field(default=8, ge=0)
    max_runtime_seconds: float = Field(default=300.0, gt=0)
    max_step_attempts: int = Field(default=3, ge=1)
    max_same_capability_calls: int = Field(default=2, ge=1)


class PlannerDecision(BaseModel):
    action: Literal[
        "continue",
        "replan",
        "request_input",
        "complete",
        "fail",
        "unsupported",
    ]
    plan: Plan | None = None
    required_input: list[str] = Field(default_factory=list)
    unsupported_intents: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_user_confirmation: bool = False
    reason: str | None = None


class Observation(BaseModel):
    observation_id: str
    task_id: str
    plan_id: str
    step_id: str
    capability: str
    status: Literal["succeeded", "failed", "unknown"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class CapabilityCall(BaseModel):
    task_id: str
    plan_id: str
    step_id: str
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    task_id: str
    plan_id: str
    step_id: str
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class TaskRecord(BaseModel):
    """Authoritative persisted Task state; PostgreSQL is the source of truth."""

    task_id: str
    source_type: Literal["chat", "knowledge_event"]
    owner_user_id: str
    status: str = "received"
    input: dict[str, Any] = Field(default_factory=dict)
    source_ref: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    current_plan_id: str | None = None
    current_plan_version: int = 0
    objective: str | None = None
    checkpoint: dict[str, Any] | None = None
    understanding: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    replan_count: int = Field(default=0, ge=0)
    step_count: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    last_error: dict[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    # Conversation history: the Task is the execution unit, the Conversation is
    # the history unit. All three are optional so Tasks created before the
    # conversation tables existed (and knowledge_event fan-out) still load.
    conversation_id: str | None = None
    request_message_id: str | None = None
    response_message_id: str | None = None
    created_at: datetime
    updated_at: datetime

    def to_envelope(self) -> TaskEnvelope:
        return TaskEnvelope(
            task_id=self.task_id,
            source_type=self.source_type,
            owner_user_id=self.owner_user_id,
            input=dict(self.input),
            source_ref=dict(self.source_ref),
            constraints=dict(self.constraints),
            created_at=self.created_at,
        )

    @classmethod
    def from_envelope(
        cls,
        task: TaskEnvelope,
        *,
        status: str = "received",
        idempotency_key: str | None = None,
    ) -> "TaskRecord":
        return cls(
            task_id=task.task_id,
            source_type=task.source_type,
            owner_user_id=task.owner_user_id,
            status=status,
            input=dict(task.input),
            source_ref=dict(task.source_ref),
            constraints=dict(task.constraints),
            idempotency_key=idempotency_key,
            created_at=task.created_at,
            updated_at=task.created_at,
        )


class TaskInput(BaseModel):
    input_id: str
    task_id: str
    version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskEvent(BaseModel):
    event_id: str
    task_id: str
    # 0 means "assigned by the store on append"; persisted events are >= 1.
    sequence: int = Field(default=0, ge=0)
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class OutboxEvent(BaseModel):
    event_id: str
    task_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    attempt_count: int = 0
    last_error: str | None = None
    available_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime


class ApprovalRecord(BaseModel):
    approval_id: str
    task_id: str
    plan_id: str
    step_id: str
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Hash of the plan-time Step arguments the user actually saw. An approval is
    # only valid for the exact arguments it was granted for: a re-plan that
    # rewrites the Step (even reusing its step_id) must ask again.
    arguments_hash: str | None = None
    version: int = Field(default=1, ge=1)
    status: str = "waiting_approval"
    reason: str | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    created_at: datetime
    updated_at: datetime


class CapabilityCallRecord(BaseModel):
    call_id: str
    task_id: str
    plan_id: str
    step_id: str
    capability: str
    idempotency_key: str
    request_id: str
    attempt: int = Field(ge=1)
    status: str = "pending"
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    finished_at: datetime | None = None


class EvidenceRecord(BaseModel):
    evidence_id: str
    task_id: str
    plan_id: str
    step_id: str
    observation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Checkpoint(BaseModel):
    task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    completed_step_ids: list[str] = Field(default_factory=list)
    next_step_id: str | None = None
    task_status: str
    updated_at: datetime


class TaskRunResult(BaseModel):
    task_id: str
    status: str
    plan_id: str | None = None
    executed_step_ids: list[str] = Field(default_factory=list)
    waiting_for: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TodoRecord(BaseModel):
    """A to-do the owner must still act on. Owned by the Agent service.

    The Agent keeps this ledger itself: there is no external calendar or
    Knowledge write behind ``todo.create``. The row is created only after the
    owner approves the draft, and it stays until the owner deletes it on the
    desktop, which is what makes an overdue to-do keep showing up.
    """

    todo_id: str
    owner_user_id: str
    title: str = Field(min_length=1, max_length=200)
    # Optional by design: "完成登录模块代码" carries no time at all, and an
    # uncertain meeting time must not block creating the to-do.
    due_at: datetime | None = None
    # The original phrase ("明天晚上八点") so the desktop can show what the owner
    # actually wrote even after the resolved timestamp is edited.
    due_expression: str | None = None
    timezone: str | None = None
    notes: str | None = None
    status: str = "open"
    source: dict[str, Any] = Field(default_factory=dict)
    plan_id: str | None = None
    step_id: str | None = None
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class ConversationRecord(BaseModel):
    """A user-visible conversation window.

    One conversation holds many Tasks. ``summary`` / ``summary_cursor`` are
    reserved for the memory phase and stay unused in Phase 1.
    """

    conversation_id: str
    owner_user_id: str
    organization_id: str | None = None
    title: str = Field(default="新的对话", max_length=200)
    status: str = "active"
    source: str = "agent"
    summary: str | None = None
    summary_cursor: int = Field(default=0, ge=0)
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MessageRecord(BaseModel):
    """A user-visible message inside a conversation.

    Only what the user sees lives here; plan steps and observations stay in
    their own tables and are rendered as the execution trace.
    """

    message_id: str
    conversation_id: str
    role: Literal["user", "assistant", "system"]
    content: str = ""
    status: str = "pending"
    task_id: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    client_message_id: str | None = None
    created_at: datetime
    updated_at: datetime
