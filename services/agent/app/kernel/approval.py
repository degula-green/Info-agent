"""Approval gate for write capabilities."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from app.kernel.errors import AgentContractError
from app.kernel.events import new_outbox_event, new_task_event, utcnow
from app.kernel.models import ApprovalRecord, PlanStep, TaskRecord
from app.kernel.protocols import AgentStore
from app.kernel.states import (
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_REJECTED,
    TERMINAL_TASK_STATUSES,
    ensure_step_transition,
    ensure_task_transition,
)


class ApprovalError(AgentContractError):
    pass


def arguments_fingerprint(arguments: dict | None) -> str:
    """Stable fingerprint of the arguments a user approved.

    Replanning may rewrite a Step while reusing its step_id; comparing this
    fingerprint before execution is what stops an old approval from silently
    authorising new write arguments.
    """

    payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approval_matches_arguments(approval: ApprovalRecord, arguments: dict | None) -> bool:
    """True when the approval was granted for exactly these arguments.

    A missing fingerprint is a legacy row; it is treated as not matching so the
    Task pauses again instead of writing arguments nobody approved.
    """

    return bool(approval.arguments_hash) and approval.arguments_hash == arguments_fingerprint(
        arguments
    )


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
            arguments_hash=arguments_fingerprint(step.arguments),
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
        # A Task the owner already finished must not be reopened by a late click
        # on its old preview. Without this, moving a lapsed decision on to the
        # transition guard below would surface as an invalid-transition crash
        # instead of a plain "no longer pending" refusal.
        if task.status in TERMINAL_TASK_STATUSES:
            raise ApprovalError(f"approval is not pending: task is {task.status}")
        # A lapsed window is not the same thing as a changed plan. The
        # fingerprint checked below is what binds a decision to the exact
        # arguments the owner was shown, so a preview the owner returns to
        # hours later stays safely confirmable: the window is refreshed
        # instead of dead-ending the card. Expiry still refuses a decision on
        # an approval that is no longer pending for any other reason.
        lapsed = approval.expires_at is not None and approval.expires_at < utcnow()
        if approval.status not in {"waiting_approval", "expired"}:
            raise ApprovalError(f"approval is not pending: {approval.status}")
        revive_window = lapsed or approval.status == "expired"
        if version is not None and version != approval.version:
            raise ApprovalError("approval version mismatch")

        step = self.store.get_step(approval.step_id)
        if step is None:
            raise ApprovalError(f"unknown step for approval: {approval.step_id}")
        # The approval is bound to the Step arguments the user actually saw. A
        # re-plan can legitimately reuse a step_id, so approving an id that has
        # since been rewritten must not execute the new arguments.
        if step.plan_id != approval.plan_id:
            raise ApprovalError("approval does not belong to the current step plan")
        if approve and not approval_matches_arguments(approval, step.arguments):
            approval.status = "superseded"
            approval.updated_at = utcnow()
            self.store.save_approval(approval)
            raise ApprovalError(
                "approval no longer matches the step arguments; a new approval is required"
            )

        moment = utcnow()
        if revive_window:
            # The owner came back to a preview whose arguments still match, so
            # the decision they are making is the one they were shown: renew
            # the window rather than rejecting the click.
            approval.expires_at = moment + timedelta(seconds=self.expires_seconds)
        approval.decided_at = moment
        approval.decided_by = owner_user_id
        approval.updated_at = moment
        events = []

        if approve:
            approval.status = "approved"
            if arguments is not None:
                approval.arguments = dict(arguments)
                step.arguments = dict(arguments)
                # The user granted this approval for the *edited* arguments, so
                # the fingerprint must follow them; otherwise the Task could
                # never execute the very change that was just confirmed.
                approval.arguments_hash = arguments_fingerprint(step.arguments)
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
