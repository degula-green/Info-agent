"""Durable queue for browser operations executed by the desktop client."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Protocol
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


TERMINAL_STATUSES = {"completed", "failed", "expired"}


@dataclass(slots=True)
class DesktopTaskRecord:
    desktop_task_id: str
    agent_task_id: str | None
    agent_step_id: str | None
    device_id: str
    operation: str
    request_payload: dict
    status: str
    result: dict | None
    error_code: str
    error_message: str
    attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    expires_at: datetime
    delivered_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    side_effect_state: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "DesktopTaskRecord":
        return cls(
            desktop_task_id=str(row["desktop_task_id"]),
            agent_task_id=(
                str(row["agent_task_id"]) if row.get("agent_task_id") else None
            ),
            agent_step_id=(
                str(row["agent_step_id"]) if row.get("agent_step_id") else None
            ),
            device_id=str(row["device_id"]),
            operation=str(row["operation"]),
            request_payload=row.get("request_payload") or {},
            status=str(row["status"]),
            result=row.get("result"),
            error_code=str(row.get("error_code") or ""),
            error_message=str(row.get("error_message") or ""),
            attempt=int(row.get("attempt") or 0),
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            expires_at=row["expires_at"],
            delivered_at=row.get("delivered_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            side_effect_state=str(row.get("side_effect_state") or "none"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class DesktopTaskStore(Protocol):
    def create_task(
        self,
        *,
        device_id: str,
        operation: str,
        request_payload: dict,
        ttl_seconds: float,
        agent_task_id: str | None = None,
        agent_step_id: str | None = None,
        side_effect: bool = False,
    ) -> DesktopTaskRecord:
        ...

    def claim_task(
        self,
        *,
        device_id: str,
        lease_seconds: float,
    ) -> DesktopTaskRecord | None:
        ...

    def get_task(self, desktop_task_id: str) -> DesktopTaskRecord | None:
        ...

    def complete_task(
        self,
        *,
        desktop_task_id: str,
        device_id: str,
        status: str,
        result: dict | None,
        error_code: str = "",
        error_message: str = "",
        side_effect_state: str = "",
    ) -> DesktopTaskRecord | None:
        ...

    def heartbeat_task(
        self,
        *,
        desktop_task_id: str,
        device_id: str,
        lease_seconds: float,
    ) -> bool:
        ...


class PostgresDesktopTaskStore:
    def __init__(self, pool, schema: str = "agent") -> None:
        self.pool = pool
        self.schema = schema

    @property
    def _table(self) -> str:
        return f"{self.schema}.desktop_tasks"

    def create_task(
        self,
        *,
        device_id: str,
        operation: str,
        request_payload: dict,
        ttl_seconds: float,
        agent_task_id: str | None = None,
        agent_step_id: str | None = None,
        side_effect: bool = False,
    ) -> DesktopTaskRecord:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=max(1.0, float(ttl_seconds)))
        side_effect_state = "pending" if side_effect else "none"
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._table} (
                        desktop_task_id, agent_task_id, agent_step_id,
                        device_id, operation, request_payload, status,
                        expires_at, side_effect_state, created_at, updated_at
                    ) VALUES (%s, NULLIF(%s,''), NULLIF(%s,''),
                              %s, %s, %s, 'pending', %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        uuid4(),
                        agent_task_id or "",
                        agent_step_id or "",
                        device_id,
                        operation,
                        Jsonb(request_payload),
                        expires_at,
                        side_effect_state,
                        now,
                        now,
                    ),
                )
                return DesktopTaskRecord.from_row(cursor.fetchone())

    def claim_task(
        self,
        *,
        device_id: str,
        lease_seconds: float,
    ) -> DesktopTaskRecord | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=max(5.0, float(lease_seconds)))
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._table}
                    SET status='expired', updated_at=%s
                    WHERE status IN ('pending','delivered','running','waiting_login')
                      AND expires_at <= %s
                    """,
                    (now, now),
                )
                cursor.execute(
                    f"""
                    WITH claimed AS (
                        SELECT desktop_task_id
                        FROM {self._table}
                        WHERE device_id=%s
                          AND status='pending'
                          AND expires_at > %s
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE {self._table} AS task
                    SET status='delivered',
                        attempt=task.attempt+1,
                        delivered_at=%s,
                        lease_owner=%s,
                        lease_expires_at=%s,
                        updated_at=%s
                    FROM claimed
                    WHERE task.desktop_task_id=claimed.desktop_task_id
                    RETURNING task.*
                    """,
                    (device_id, now, now, device_id, lease_until, now),
                )
                row = cursor.fetchone()
                return DesktopTaskRecord.from_row(row) if row else None

    def get_task(self, desktop_task_id: str) -> DesktopTaskRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._table} WHERE desktop_task_id=%s",
                    (desktop_task_id,),
                )
                row = cursor.fetchone()
                return DesktopTaskRecord.from_row(row) if row else None

    def complete_task(
        self,
        *,
        desktop_task_id: str,
        device_id: str,
        status: str,
        result: dict | None,
        error_code: str = "",
        error_message: str = "",
        side_effect_state: str = "",
    ) -> DesktopTaskRecord | None:
        now = datetime.now(timezone.utc)
        side_effect = side_effect_state.strip()
        if not side_effect:
            side_effect = "committed" if status == "completed" else "none"
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._table}
                    SET status=%s,
                        result=%s,
                        error_code=NULLIF(%s,''),
                        error_message=NULLIF(%s,''),
                        completed_at=CASE
                            WHEN %s IN ('completed','failed','expired','needs_review')
                            THEN %s ELSE completed_at END,
                        side_effect_state=%s,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        updated_at=%s
                    WHERE desktop_task_id=%s
                      AND device_id=%s
                      AND status NOT IN ('completed','failed','expired')
                    RETURNING *
                    """,
                    (
                        status,
                        Jsonb(result) if result is not None else None,
                        error_code,
                        error_message,
                        status,
                        now,
                        side_effect,
                        now,
                        desktop_task_id,
                        device_id,
                    ),
                )
                row = cursor.fetchone()
                return DesktopTaskRecord.from_row(row) if row else None

    def heartbeat_task(
        self,
        *,
        desktop_task_id: str,
        device_id: str,
        lease_seconds: float,
    ) -> bool:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=max(5.0, float(lease_seconds)))
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._table}
                    SET lease_expires_at=%s, updated_at=%s
                    WHERE desktop_task_id=%s
                      AND device_id=%s
                      AND status IN ('delivered','running','waiting_login')
                    """,
                    (lease_until, now, desktop_task_id, device_id),
                )
                return cursor.rowcount > 0


class InMemoryDesktopTaskStore:
    """Deterministic store used by tests and local development without Postgres."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[str, DesktopTaskRecord] = {}

    @staticmethod
    def _copy(record: DesktopTaskRecord) -> DesktopTaskRecord:
        return replace(
            record,
            request_payload=dict(record.request_payload),
            result=dict(record.result) if record.result else None,
        )

    def create_task(
        self,
        *,
        device_id: str,
        operation: str,
        request_payload: dict,
        ttl_seconds: float,
        agent_task_id: str | None = None,
        agent_step_id: str | None = None,
        side_effect: bool = False,
    ) -> DesktopTaskRecord:
        now = datetime.now(timezone.utc)
        record = DesktopTaskRecord(
            desktop_task_id=str(uuid4()),
            agent_task_id=agent_task_id,
            agent_step_id=agent_step_id,
            device_id=device_id,
            operation=operation,
            request_payload=dict(request_payload),
            status="pending",
            result=None,
            error_code="",
            error_message="",
            attempt=0,
            lease_owner=None,
            lease_expires_at=None,
            expires_at=now + timedelta(seconds=max(1.0, float(ttl_seconds))),
            delivered_at=None,
            started_at=None,
            completed_at=None,
            side_effect_state="pending" if side_effect else "none",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._tasks[record.desktop_task_id] = record
        return self._copy(record)

    def claim_task(
        self,
        *,
        device_id: str,
        lease_seconds: float,
    ) -> DesktopTaskRecord | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=max(5.0, float(lease_seconds)))
        with self._lock:
            for task_id, record in list(self._tasks.items()):
                if record.status in {"pending", "delivered", "running", "waiting_login"} and record.expires_at <= now:
                    record.status = "expired"
                    record.updated_at = now
                    self._tasks[task_id] = record
            for task_id, record in sorted(
                self._tasks.items(), key=lambda item: item[1].created_at
            ):
                if (
                    record.device_id == device_id
                    and record.status == "pending"
                    and record.expires_at > now
                ):
                    record.status = "delivered"
                    record.attempt += 1
                    record.delivered_at = now
                    record.lease_owner = device_id
                    record.lease_expires_at = lease_until
                    record.updated_at = now
                    self._tasks[task_id] = record
                    return self._copy(record)
        return None

    def get_task(self, desktop_task_id: str) -> DesktopTaskRecord | None:
        with self._lock:
            record = self._tasks.get(desktop_task_id)
            return self._copy(record) if record else None

    def complete_task(
        self,
        *,
        desktop_task_id: str,
        device_id: str,
        status: str,
        result: dict | None,
        error_code: str = "",
        error_message: str = "",
        side_effect_state: str = "",
    ) -> DesktopTaskRecord | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            record = self._tasks.get(desktop_task_id)
            if (
                record is None
                or record.device_id != device_id
                or record.status in TERMINAL_STATUSES
            ):
                return None
            record.status = status
            record.result = dict(result) if result is not None else None
            record.error_code = error_code
            record.error_message = error_message
            record.completed_at = (
                now
                if status in {"completed", "failed", "expired", "needs_review"}
                else record.completed_at
            )
            record.side_effect_state = (
                side_effect_state
                or ("committed" if status == "completed" else "none")
            )
            record.lease_owner = None
            record.lease_expires_at = None
            record.updated_at = now
            self._tasks[desktop_task_id] = record
            return self._copy(record)

    def heartbeat_task(
        self,
        *,
        desktop_task_id: str,
        device_id: str,
        lease_seconds: float,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            record = self._tasks.get(desktop_task_id)
            if (
                record is None
                or record.device_id != device_id
                or record.status
                not in {"delivered", "running", "waiting_login"}
            ):
                return False
            record.lease_expires_at = now + timedelta(
                seconds=max(5.0, float(lease_seconds))
            )
            record.updated_at = now
            self._tasks[desktop_task_id] = record
            return True
