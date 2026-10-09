"""In-memory implementation of the AgentStore protocol.

Used by unit and integration tests so the kernel can be verified without a real
PostgreSQL or Redis instance. Behaviour mirrors the PostgreSQL store.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.kernel.events import utcnow
from app.kernel.models import (
    ApprovalRecord,
    CapabilityCallRecord,
    ConversationRecord,
    ConversationSummaryJob,
    EvidenceRecord,
    MessageRecord,
    MemoryRecord,
    MemorySourceRecord,
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
        self.conversations: dict[str, ConversationRecord] = {}
        self.messages: dict[str, MessageRecord] = {}
        self.summary_jobs: dict[str, ConversationSummaryJob] = {}
        self.memories: dict[str, MemoryRecord] = {}
        self.memory_sources: dict[str, list[MemorySourceRecord]] = {}
        self.person_fact_snapshots: dict[
            tuple[str, str, str], list[dict[str, Any]]
        ] = {}

    def get_person_fact_snapshot(
        self,
        *,
        owner_user_id: str,
        person_key: str,
        snapshot_fingerprint: str,
    ) -> list[dict[str, Any]] | None:
        with self._lock:
            value = self.person_fact_snapshots.get(
                (owner_user_id, person_key, snapshot_fingerprint)
            )
            return [dict(item) for item in value] if value is not None else None

    def save_person_fact_snapshot(
        self,
        *,
        owner_user_id: str,
        person_key: str,
        snapshot_fingerprint: str,
        facts: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self.person_fact_snapshots[
                (owner_user_id, person_key, snapshot_fingerprint)
            ] = [dict(item) for item in facts]

    # -- tasks ------------------------------------------------------------

    def create_task(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent] | None = None,
        outbox_events: list[OutboxEvent] | None = None,
        inputs: list[TaskInput] | None = None,
        conversation: ConversationRecord | None = None,
        messages: list[MessageRecord] | None = None,
    ) -> TaskRecord:
        with self._lock:
            if task.idempotency_key:
                existing_id = self.idempotency.get(task.idempotency_key)
                if existing_id is not None:
                    return self.tasks[existing_id].model_copy(deep=True)
                self.idempotency[task.idempotency_key] = task.task_id
            if conversation is not None:
                self.conversations[conversation.conversation_id] = (
                    conversation.model_copy(deep=True)
                )
            self.tasks[task.task_id] = task.model_copy(deep=True)
            self.inputs.setdefault(task.task_id, [])
            self.observations.setdefault(task.task_id, [])
            self.events.setdefault(task.task_id, [])
            for item in inputs or []:
                self.add_input(item)
            for message in messages or []:
                self.messages[message.message_id] = message.model_copy(deep=True)
                stored_conversation = self.conversations.get(message.conversation_id)
                if stored_conversation is not None:
                    stored_conversation.last_message_at = message.created_at
                    stored_conversation.updated_at = message.created_at
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

    def list_tasks_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        with self._lock:
            wanted = {status for status in (statuses or []) if status}
            tasks = [
                task
                for task in self.tasks.values()
                if task.owner_user_id == owner_user_id and (not wanted or task.status in wanted)
            ]
            tasks.sort(key=lambda item: item.created_at, reverse=True)
            return [task.model_copy(deep=True) for task in tasks[: max(0, limit)]]

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

    def renew_lease(self, task_id: str, owner: str, seconds: float) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.lease_owner != owner:
                return False
            task.lease_expires_at = utcnow() + timedelta(seconds=seconds)
            return True

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

    def next_plan_version(self, task_id: str) -> int:
        with self._lock:
            return max(
                (plan.version for plan in self.plans.values() if plan.task_id == task_id),
                default=0,
            ) + 1

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
            # The PostgreSQL store persists these columns; the double must do the
            # same or retry accounting silently diverges from production.
            if attempt_count is not None:
                stored.attempt_count = attempt_count
            stored.replaced_by_step_id = step.replaced_by_step_id
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

    def find_capability_call_for_step(
        self, task_id: str, step_id: str
    ) -> CapabilityCallRecord | None:
        with self._lock:
            matches = [
                call
                for call in self.calls.values()
                if call.task_id == task_id and call.step_id == step_id
            ]
        if not matches:
            return None
        matches.sort(key=lambda item: (item.attempt, item.created_at))
        return matches[-1].model_copy(deep=True)

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

    def claim_outbox(
        self, limit: int = 50, *, lease_seconds: float = 60.0
    ) -> list[OutboxEvent]:
        with self._lock:
            now = utcnow()
            items = [
                item
                for item in self.outbox.values()
                if item.status in {"pending", "publishing"}
                and (item.available_at is None or item.available_at <= now)
            ]
            items.sort(key=lambda item: item.created_at)
            claimed = items[: max(1, int(limit))]
            if not claimed:
                return []
            lease_until = now + timedelta(seconds=max(1.0, float(lease_seconds)))
            for item in claimed:
                item.status = "publishing"
                item.available_at = lease_until
            return [item.model_copy(deep=True) for item in claimed]

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

    # -- conversation history ---------------------------------------------

    def create_conversation(
        self, conversation: ConversationRecord
    ) -> ConversationRecord:
        with self._lock:
            self.conversations[conversation.conversation_id] = (
                conversation.model_copy(deep=True)
            )
            return conversation.model_copy(deep=True)

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._lock:
            item = self.conversations.get(conversation_id)
            return item.model_copy(deep=True) if item else None

    def save_conversation(self, conversation: ConversationRecord) -> None:
        with self._lock:
            stored = conversation.model_copy(deep=True)
            stored.updated_at = utcnow()
            self.conversations[stored.conversation_id] = stored

    def list_conversations_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationRecord]:
        with self._lock:
            items = [
                item
                for item in self.conversations.values()
                if item.owner_user_id == owner_user_id
                and (statuses is None or item.status in statuses)
            ]
            items.sort(
                key=lambda item: item.last_message_at or item.created_at,
                reverse=True,
            )
            start = max(int(offset), 0)
            return [
                item.model_copy(deep=True)
                for item in items[start : start + max(int(limit), 1)]
            ]

    def count_conversations_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
    ) -> int:
        with self._lock:
            return sum(
                1
                for item in self.conversations.values()
                if item.owner_user_id == owner_user_id
                and (statuses is None or item.status in statuses)
            )

    def delete_conversation(
        self, conversation_id: str, *, owner_user_id: str
    ) -> bool:
        with self._lock:
            item = self.conversations.get(conversation_id)
            if item is None or item.owner_user_id != owner_user_id:
                return False
            del self.conversations[conversation_id]
            for message_id in [
                message.message_id
                for message in self.messages.values()
                if message.conversation_id == conversation_id
            ]:
                del self.messages[message_id]
            for job_id in [
                job.job_id
                for job in self.summary_jobs.values()
                if job.conversation_id == conversation_id
            ]:
                del self.summary_jobs[job_id]
            for memory_id in [
                memory.memory_id
                for memory in self.memories.values()
                if memory.source_conversation_id == conversation_id
            ]:
                self.memories.pop(memory_id, None)
                self.memory_sources.pop(memory_id, None)
            return True

    def add_message(self, message: MessageRecord) -> MessageRecord:
        with self._lock:
            self.messages[message.message_id] = message.model_copy(deep=True)
            conversation = self.conversations.get(message.conversation_id)
            if conversation is not None:
                conversation.last_message_at = message.created_at
            return message.model_copy(deep=True)

    def get_message(self, message_id: str) -> MessageRecord | None:
        with self._lock:
            item = self.messages.get(message_id)
            return item.model_copy(deep=True) if item else None

    def update_message(self, message: MessageRecord) -> None:
        with self._lock:
            stored = message.model_copy(deep=True)
            stored.updated_at = utcnow()
            self.messages[stored.message_id] = stored

    def list_messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[MessageRecord]:
        with self._lock:
            items = [
                item
                for item in self.messages.values()
                if item.conversation_id == conversation_id
            ]
            items.sort(key=lambda item: item.created_at)
            if limit is not None:
                items = items[:limit]
            return [item.model_copy(deep=True) for item in items]

    def count_messages(self, conversation_id: str) -> int:
        with self._lock:
            return sum(
                1
                for item in self.messages.values()
                if item.conversation_id == conversation_id
            )

    def count_messages_by_conversation(
        self, conversation_ids: list[str]
    ) -> dict[str, int]:
        wanted = {str(item) for item in conversation_ids if item}
        if not wanted:
            return {}
        counts = {item: 0 for item in wanted}
        with self._lock:
            for message in self.messages.values():
                key = str(message.conversation_id)
                if key in counts:
                    counts[key] += 1
        return counts

    def list_completed_messages_after_boundary(
        self,
        conversation_id: str,
        *,
        boundary_message_id: str | None = None,
        boundary_to_message_id: str | None = None,
        exclude_task_id: str | None = None,
        limit: int | None = None,
    ) -> list[MessageRecord]:
        with self._lock:
            boundary = (
                self.messages.get(boundary_message_id)
                if boundary_message_id
                else None
            )
            if boundary_message_id and boundary is None:
                return []
            boundary_to = (
                self.messages.get(boundary_to_message_id)
                if boundary_to_message_id
                else None
            )
            if boundary_to_message_id and boundary_to is None:
                return []
            items = []
            for item in self.messages.values():
                if item.conversation_id != conversation_id:
                    continue
                if item.status != "completed":
                    continue
                if exclude_task_id is not None and item.task_id == exclude_task_id:
                    continue
                if boundary is not None:
                    if (item.created_at, item.message_id) <= (
                        boundary.created_at,
                        boundary.message_id,
                    ):
                        continue
                if boundary_to is not None:
                    if (item.created_at, item.message_id) > (
                        boundary_to.created_at,
                        boundary_to.message_id,
                    ):
                        continue
                items.append(item.model_copy(deep=True))
            items.sort(key=lambda item: (item.created_at, item.message_id))
            if limit is not None:
                items = items[: max(0, int(limit))]
            return items

    def create_conversation_summary_job(
        self, job: ConversationSummaryJob
    ) -> ConversationSummaryJob:
        with self._lock:
            for existing in self.summary_jobs.values():
                if (
                    existing.conversation_id == job.conversation_id
                    and existing.expected_summary_version == job.expected_summary_version
                    and existing.boundary_to_message_id == job.boundary_to_message_id
                ):
                    return existing.model_copy(deep=True)
            stored = job.model_copy(deep=True)
            self.summary_jobs[stored.job_id] = stored
            return stored.model_copy(deep=True)

    def claim_conversation_summary_jobs(
        self,
        *,
        owner: str,
        limit: int = 10,
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> list[ConversationSummaryJob]:
        with self._lock:
            now = utcnow()
            claimable = [
                job
                for job in self.summary_jobs.values()
                if job.status in {"pending", "failed"}
                and job.available_at <= now
                and job.attempt_count < max(1, int(max_attempts))
                and (
                    job.lease_until is None
                    or job.lease_until <= now
                    or job.lease_owner == owner
                )
            ]
            claimable.sort(key=lambda item: item.created_at)
            claimed: list[ConversationSummaryJob] = []
            for job in claimable[: max(1, int(limit))]:
                job.status = "running"
                job.lease_owner = owner
                job.lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
                job.updated_at = now
                claimed.append(job.model_copy(deep=True))
            return claimed

    def complete_conversation_summary_job(
        self, job_id: str, *, owner: str, finished_at: datetime
    ) -> None:
        with self._lock:
            job = self.summary_jobs.get(job_id)
            if job is None or job.lease_owner != owner:
                return
            job.status = "succeeded"
            job.lease_owner = None
            job.lease_until = None
            job.finished_at = finished_at
            job.updated_at = finished_at

    def fail_conversation_summary_job(
        self,
        job_id: str,
        *,
        owner: str,
        error: str,
        available_at: datetime,
    ) -> None:
        with self._lock:
            job = self.summary_jobs.get(job_id)
            if job is None or job.lease_owner != owner:
                return
            job.status = "failed"
            job.attempt_count += 1
            job.last_error = error[:1000]
            job.available_at = available_at
            job.lease_owner = None
            job.lease_until = None
            job.updated_at = utcnow()

    def compare_and_set_conversation_summary(
        self,
        *,
        conversation_id: str,
        expected_version: int,
        boundary_from_message_id: str | None,
        boundary_to_message_id: str,
        summary: str,
        summary_token_count: int,
        summary_method: str,
        updated_at: datetime,
    ) -> bool:
        with self._lock:
            conversation = self.conversations.get(conversation_id)
            if conversation is None or conversation.summary_version != expected_version:
                return False
            conversation.summary = summary
            conversation.summary_cursor += 1
            conversation.summary_until_message_id = boundary_to_message_id
            conversation.summary_version += 1
            conversation.summary_updated_at = updated_at
            conversation.summary_method = summary_method
            conversation.summary_token_count = max(0, int(summary_token_count))
            conversation.updated_at = updated_at
            return True

    def create_memory(self, memory: MemoryRecord) -> MemoryRecord:
        with self._lock:
            if memory.status == "active":
                for existing in self.memories.values():
                    if (
                        existing.owner_user_id == memory.owner_user_id
                        and existing.source_conversation_id
                        == memory.source_conversation_id
                        and existing.memory_type == memory.memory_type
                        and existing.memory_key == memory.memory_key
                        and existing.status == "active"
                    ):
                        return existing.model_copy(deep=True)
            stored = memory.model_copy(deep=True)
            self.memories[stored.memory_id] = stored
            self.memory_sources.setdefault(stored.memory_id, [])
            return stored.model_copy(deep=True)

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            memory = self.memories.get(memory_id)
            return memory.model_copy(deep=True) if memory else None

    def list_memories(
        self,
        owner_user_id: str,
        *,
        conversation_id: str,
        statuses: list[str] | None = None,
        memory_types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        with self._lock:
            memories = [
                memory
                for memory in self.memories.values()
                if memory.owner_user_id == owner_user_id
                and memory.source_conversation_id == conversation_id
                and (statuses is None or memory.status in statuses)
                and (memory_types is None or memory.memory_type in memory_types)
            ]
            memories.sort(
                key=lambda item: (item.importance, item.updated_at),
                reverse=True,
            )
            start = max(0, int(offset))
            return [
                memory.model_copy(deep=True)
                for memory in memories[start : start + max(1, int(limit))]
            ]

    def count_memories(
        self,
        owner_user_id: str,
        *,
        conversation_id: str,
        statuses: list[str] | None = None,
        memory_types: list[str] | None = None,
    ) -> int:
        with self._lock:
            return sum(
                1
                for memory in self.memories.values()
                if memory.owner_user_id == owner_user_id
                and memory.source_conversation_id == conversation_id
                and (statuses is None or memory.status in statuses)
                and (memory_types is None or memory.memory_type in memory_types)
            )

    def search_memories(
        self,
        owner_user_id: str,
        *,
        conversation_id: str,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        normalized = str(query or "").strip().lower()
        tokens = [item for item in normalized.split() if item]
        ranked: list[tuple[float, MemoryRecord]] = []
        with self._lock:
            for memory in self.memories.values():
                if (
                    memory.owner_user_id != owner_user_id
                    or memory.source_conversation_id != conversation_id
                    or memory.status != "active"
                ):
                    continue
                haystack = f"{memory.title}\n{memory.content}".lower()
                keyword_hit = any(
                    keyword.lower() in normalized
                    for keyword in memory.keywords
                    if keyword
                )
                text_hit = normalized and normalized in haystack
                token_hit = any(token in haystack for token in tokens)
                if normalized and not (keyword_hit or text_hit or token_hit):
                    continue
                score = (
                    float(memory.importance) * 0.6
                    + float(memory.confidence) * 0.4
                )
                ranked.append((score, memory.model_copy(deep=True)))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [memory for _, memory in ranked[: max(0, int(limit))]]

    def delete_memory(
        self,
        memory_id: str,
        *,
        owner_user_id: str,
        conversation_id: str,
    ) -> bool:
        with self._lock:
            memory = self.memories.get(memory_id)
            if (
                memory is None
                or memory.owner_user_id != owner_user_id
                or memory.source_conversation_id != conversation_id
            ):
                return False
            del self.memories[memory_id]
            self.memory_sources.pop(memory_id, None)
            return True

    def add_memory_sources(self, sources: list[MemorySourceRecord]) -> None:
        with self._lock:
            for source in sources:
                bucket = self.memory_sources.setdefault(source.memory_id, [])
                if not any(item.message_id == source.message_id for item in bucket):
                    bucket.append(source.model_copy(deep=True))

    def list_memory_sources(self, memory_id: str) -> list[MemorySourceRecord]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self.memory_sources.get(memory_id, [])
            ]
