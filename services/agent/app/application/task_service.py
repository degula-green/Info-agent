"""Task lifecycle services backing the Agent API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.errors import AgentContractError
from app.kernel.events import new_outbox_event, new_task_event, utcnow
from app.kernel.models import (
    Observation,
    Plan,
    TaskEvent,
    TaskInput,
    TaskRecord,
)
from app.kernel.protocols import AgentStore
from app.kernel.states import (
    EVENT_TASK_ACCEPTED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_INPUT_RECEIVED,
    TERMINAL_TASK_STATUSES,
    ensure_task_transition,
)


class TaskNotFoundError(AgentContractError):
    pass


class TaskPermissionError(AgentContractError):
    pass


class TaskStateError(AgentContractError):
    pass


class TaskService:
    def __init__(self, store: AgentStore) -> None:
        self.store = store

    def create_task(
        self,
        *,
        owner_user_id: str,
        source_type: str = "chat",
        payload: dict[str, Any],
        source_ref: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        client_message_id: str | None = None,
        task_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TaskRecord:
        moment = created_at or datetime.now(timezone.utc)
        resolved_id = task_id or str(uuid4())
        idempotency_key = (
            f"{source_type}:{owner_user_id}:{client_message_id}" if client_message_id else None
        )
        if idempotency_key:
            existing = self.store.find_task_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing

        record = TaskRecord(
            task_id=resolved_id,
            source_type=source_type,
            owner_user_id=owner_user_id,
            status="received",
            input=dict(payload),
            source_ref=dict(source_ref or {}),
            constraints=dict(constraints or {}),
            idempotency_key=idempotency_key,
            created_at=moment,
            updated_at=moment,
        )
        events = [
            new_task_event(
                resolved_id,
                EVENT_TASK_ACCEPTED,
                {"source_type": source_type, "owner_user_id": owner_user_id},
                occurred_at=moment,
            )
        ]
        outbox = [new_outbox_event(resolved_id, "agent.task.wakeup", {"reason": "accepted"}, created_at=moment)]
        task = self.store.create_task(record, events=events, outbox_events=outbox)
        self.store.add_input(
            TaskInput(
                input_id=str(uuid4()),
                task_id=task.task_id,
                version=1,
                payload=dict(payload),
                created_at=moment,
            )
        )
        return task

    def get_task(self, task_id: str, *, owner_user_id: str | None = None) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"unknown task: {task_id}")
        if owner_user_id is not None and task.owner_user_id != owner_user_id:
            raise TaskPermissionError("task does not belong to the current user")
        return task

    def get_active_plan(self, task_id: str) -> Plan | None:
        return self.store.get_active_plan(task_id)

    def list_observations(self, task_id: str) -> list[Observation]:
        return self.store.list_observations(task_id)

    def list_events(self, task_id: str, *, after_sequence: int = 0) -> list[TaskEvent]:
        return self.store.list_events(task_id, after_sequence=after_sequence)

    def submit_input(
        self, task_id: str, *, owner_user_id: str, payload: dict[str, Any]
    ) -> TaskRecord:
        task = self.get_task(task_id, owner_user_id=owner_user_id)
        if task.status != "waiting_input":
            raise TaskStateError(f"task is not waiting for input: {task.status}")

        moment = utcnow()
        versions = self.store.list_inputs(task_id)
        next_version = (versions[-1].version + 1) if versions else 1
        self.store.add_input(
            TaskInput(
                input_id=str(uuid4()),
                task_id=task_id,
                version=next_version,
                payload=dict(payload),
                created_at=moment,
            )
        )

        # The unfinished plan belonged to the incomplete input, so it is
        # invalidated and the runtime re-plans from the accumulated input.
        self.store.invalidate_plans(task_id)
        merged = dict(task.input)
        merged.update(payload)
        task.input = merged
        task.current_plan_id = None
        ensure_task_transition(task.status, "planning")
        task.status = "planning"
        task.updated_at = moment
        self.store.commit(
            task,
            events=[
                new_task_event(
                    task_id,
                    EVENT_TASK_INPUT_RECEIVED,
                    {"version": next_version, "payload": payload},
                    occurred_at=moment,
                )
            ],
            outbox_events=[
                new_outbox_event(task_id, "agent.task.wakeup", {"reason": "input_received"}, created_at=moment)
            ],
        )
        updated = self.store.get_task(task_id)
        assert updated is not None
        return updated

    def cancel(self, task_id: str, *, owner_user_id: str) -> TaskRecord:
        task = self.get_task(task_id, owner_user_id=owner_user_id)
        if task.status in TERMINAL_TASK_STATUSES:
            raise TaskStateError(f"task is already finished: {task.status}")
        moment = utcnow()
        ensure_task_transition(task.status, "cancelled")
        task.status = "cancelled"
        task.updated_at = moment
        self.store.commit(
            task,
            events=[new_task_event(task_id, EVENT_TASK_CANCELLED, {}, occurred_at=moment)],
        )
        updated = self.store.get_task(task_id)
        assert updated is not None
        return updated
