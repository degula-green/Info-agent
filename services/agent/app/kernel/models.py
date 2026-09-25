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


class TaskUnderstanding(BaseModel):
    goal: str
    intent: str | None = None
    entities: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class PlanStep(BaseModel):
    step_id: str
    plan_id: str
    order: int = Field(ge=1)
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    attempt_count: int = Field(default=0, ge=0)


class Plan(BaseModel):
    plan_id: str
    task_id: str
    version: int = Field(default=1, ge=1)
    objective: str
    steps: list[PlanStep] = Field(default_factory=list)
    status: str = "draft"


class CapabilityDescriptor(BaseModel):
    name: str
    description: str
    risk_level: str
    side_effect: bool
    requires_approval: bool
    idempotent: bool
    timeout_seconds: int = Field(gt=0)


class PolicyDecision(BaseModel):
    action: Literal["allow", "deny", "require_approval"]
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
    last_error: dict[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
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
