"""Task Event and Outbox Event construction helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.models import OutboxEvent, TaskEvent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_task_event(
    task_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    occurred_at: datetime | None = None,
    sequence: int = 0,
) -> TaskEvent:
    return TaskEvent(
        event_id=str(uuid4()),
        task_id=task_id,
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
        occurred_at=occurred_at or utcnow(),
    )


def new_outbox_event(
    task_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    created_at: datetime | None = None,
) -> OutboxEvent:
    moment = created_at or utcnow()
    return OutboxEvent(
        event_id=str(uuid4()),
        task_id=task_id,
        event_type=event_type,
        payload=payload or {},
        available_at=moment,
        created_at=moment,
    )


def wake_up_event(task_id: str, reason: str) -> OutboxEvent:
    return new_outbox_event(task_id, "agent.task.wakeup", {"task_id": task_id, "reason": reason})
