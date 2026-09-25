"""Approval gate for write capabilities."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.kernel.errors import AgentContractError
from app.kernel.events import new_outbox_event, new_task_event, utcnow
from app.kernel.models import ApprovalRecord, PlanStep, TaskRecord
from app.kernel.protocols import AgentStore
from app.kernel.states import (
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_REJECTED,
    ensure_step_transition,
    ensure_task_transition,
)


class ApprovalError(AgentContractError):
    pass


class ApprovalGateway:
    def __init__(self, store: AgentStore, *, expires_seconds: float = 3600.0) -> None:
        self.store = store
        self.expires_seconds = expires_seconds

    def request(
        self,
        task: TaskRecord,
        step: PlanStep,
        *,
        reason: str | None,
        version: int = 1,
    ) -> ApprovalRecord:
        moment = utcnow()
        approval = ApprovalRecord(
            approval_id=str(uuid4()),
            task_id=task.task_id,
            plan_id=step.plan_id,
            step_id=step.step_id,
            capability=step.capability,
            arguments=dict(step.arguments),
            version=version,
            status="waiting_approval",
            reason=reason,
            expires_at=moment + timedelta(seconds=self.expires_seconds),
            created_at=moment,
            updated_at=moment,
        )
        self.store.save_approval(approval)
        return approval

    def decide(
        self,
        approval_id: str,
        *,
        owner_user_id: str,
        approve: bool,
        version: int | None = None,
        arguments: dict | None = None,
    ) -> ApprovalRecord:
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise ApprovalError(f"unknown approval: {approval_id}")
        task = self.store.get_task(approval.task_id)
        if task is None:
            raise ApprovalError(f"unknown task for approval: {approval.task_id}")
        if task.owner_user_id != owner_user_id:
            raise ApprovalError("approval does not belong to the current user")
        if approval.status != "waiting_approval":
            raise ApprovalError(f"approval is not pending: {approval.status}")
        if approval.expires_at is not None and approval.expires_at < utcnow():
            approval.status = "expired"
            approval.updated_at = utcnow()
            self.store.save_approval(approval)
            raise ApprovalError("approval has expired")
        if version is not None and version != approval.version:
            raise ApprovalError("approval version mismatch")

        step = self.store.get_step(approval.step_id)
        if step is None:
            raise ApprovalError(f"unknown step for approval: {approval.step_id}")

        moment = utcnow()
        approval.decided_at = moment
        approval.decided_by = owner_user_id
        approval.updated_at = moment
        events = []

        if approve:
            approval.status = "approved"
            if arguments is not None:
                approval.arguments = dict(arguments)
                step.arguments = dict(arguments)
            ensure_step_transition(step.status, "ready")
            step.status = "ready"
            self.store.update_step(step, approved_version=approval.version)
            ensure_task_transition(task.status, "ready")
            task.status = "ready"
            events.append(
                new_task_event(
                    task.task_id,
                    EVENT_APPROVAL_APPROVED,
                    {"approval_id": approval.approval_id, "step_id": step.step_id, "version": approval.version},
                    occurred_at=moment,
                )
            )
        else:
            approval.status = "rejected"
            ensure_step_transition(step.status, "skipped")
            step.status = "skipped"
            self.store.update_step(step)
            ensure_task_transition(task.status, "failed")
            task.status = "failed"
            task.last_error = {"classification": "policy_denied", "message": "approval rejected"}
            events.append(
                new_task_event(
                    task.task_id,
                    EVENT_APPROVAL_REJECTED,
                    {"approval_id": approval.approval_id, "step_id": step.step_id},
                    occurred_at=moment,
                )
            )

        self.store.save_approval(approval)
        task.updated_at = moment
        self.store.commit(
            task,
            events=events,
            outbox_events=[new_outbox_event(task.task_id, "agent.task.wakeup", {"reason": "approval_decided"})]
            if approve
            else [],
        )
        return approval
