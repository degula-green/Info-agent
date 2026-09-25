"""PostgreSQL implementation of the AgentStore protocol.

PostgreSQL owns the authoritative Agent state. ``commit`` writes the Task
transition, its Task Events and its Outbox Events inside a single transaction,
which is what makes the Codex-style event stream crash safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.kernel.models import (
    ApprovalRecord,
    CapabilityCallRecord,
    EvidenceRecord,
    Observation,
    OutboxEvent,
    Plan,
    PlanStep,
    TaskEvent,
    TaskInput,
    TaskRecord,
)


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


class PostgresAgentStore:
    def __init__(self, pool: ConnectionPool, schema: str = "agent") -> None:
        self.pool = pool
        self.schema = schema

    # -- helpers ----------------------------------------------------------

    @property
    def _tasks(self) -> str:
        return f"{self.schema}.agent_tasks"

    @property
    def _inputs(self) -> str:
        return f"{self.schema}.agent_task_inputs"

    @property
    def _plans(self) -> str:
        return f"{self.schema}.agent_plans"

    @property
    def _steps(self) -> str:
        return f"{self.schema}.agent_plan_steps"

    @property
    def _observations(self) -> str:
        return f"{self.schema}.agent_observations"

    @property
    def _approvals(self) -> str:
        return f"{self.schema}.agent_approvals"

    @property
    def _calls(self) -> str:
        return f"{self.schema}.agent_capability_calls"

    @property
    def _evidence(self) -> str:
        return f"{self.schema}.agent_evidence"

    @property
    def _outbox(self) -> str:
        return f"{self.schema}.agent_outbox_events"

    @property
    def _events(self) -> str:
        return f"{self.schema}.agent_task_events"

    @staticmethod
    def _task_model(row: dict[str, Any]) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            source_type=row["source_type"],
            owner_user_id=row["owner_user_id"],
            status=row["status"],
            objective=row["objective"],
            current_plan_id=row["current_plan_id"],
            current_plan_version=row["current_plan_version"],
            idempotency_key=row["idempotency_key"],
            checkpoint=row["checkpoint"],
            last_error=row["last_error"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _plan_model(row: dict[str, Any]) -> Plan:
        return Plan(
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            version=row["version"],
            objective=row["objective"],
            status=row["status"],
            steps=[],
        )

    @staticmethod
    def _step_model(row: dict[str, Any]) -> PlanStep:
        return PlanStep(
            step_id=row["step_id"],
            plan_id=row["plan_id"],
            order=row["step_order"],
            capability=row["capability"],
            arguments=row["arguments"] or {},
            status=row["status"],
        )

    @staticmethod
    def _observation_model(row: dict[str, Any]) -> Observation:
        return Observation(
            observation_id=row["observation_id"],
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            capability=row["capability"],
            status=row["status"],
            output=row["output"],
            error=row["error"],
            evidence=row["evidence"] or [],
            created_at=row["created_at"],
        )

    @staticmethod
    def _approval_model(row: dict[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            capability=row["capability"],
            arguments=row["arguments"] or {},
            version=row["version"],
            status=row["status"],
            reason=row["reason"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _call_model(row: dict[str, Any]) -> CapabilityCallRecord:
        return CapabilityCallRecord(
            call_id=row["call_id"],
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            capability=row["capability"],
            idempotency_key=row["idempotency_key"],
            request_id=row["request_id"],
            attempt=row["attempt"],
            status=row["status"],
            arguments=row["arguments"] or {},
            result=row["result"],
            error=row["error"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _event_model(row: dict[str, Any]) -> TaskEvent:
        return TaskEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            payload=row["payload"] or {},
            occurred_at=row["occurred_at"],
        )

    @staticmethod
    def _outbox_model(row: dict[str, Any]) -> OutboxEvent:
        return OutboxEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            payload=row["payload"] or {},
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
            available_at=row["available_at"],
            published_at=row["published_at"],
            created_at=row["created_at"],
        )

    def _insert_event(self, cursor, event: TaskEvent) -> TaskEvent:
        cursor.execute(
            f"UPDATE {self._tasks} SET next_event_sequence = next_event_sequence + 1 "
            "WHERE task_id = %s RETURNING (next_event_sequence - 1) AS sequence",
            (event.task_id,),
        )
        sequence_row = cursor.fetchone()
        if sequence_row is None:
            sequence = event.sequence or 1
        elif isinstance(sequence_row, dict):
            sequence = int(sequence_row["sequence"])
        else:
            sequence = int(sequence_row[0])
        stored = event.model_copy(deep=True)
        stored.sequence = sequence
        cursor.execute(
            f"INSERT INTO {self._events} (event_id, task_id, sequence, event_type, payload, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                stored.event_id,
                stored.task_id,
                stored.sequence,
                stored.event_type,
                _json(stored.payload),
                stored.occurred_at,
            ),
        )
        return stored

    def _insert_outbox(self, cursor, event: OutboxEvent) -> None:
        moment = event.available_at or event.created_at
        cursor.execute(
            f"INSERT INTO {self._outbox} "
            "(event_id, task_id, event_type, payload, status, attempt_count, last_error, available_at, published_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (event_id) DO NOTHING",
            (
                event.event_id,
                event.task_id,
                event.event_type,
                _json(event.payload),
                event.status,
                event.attempt_count,
                event.last_error,
                moment,
                event.published_at,
                event.created_at,
            ),
        )

    def _upsert_task(self, cursor, task: TaskRecord) -> None:
        cursor.execute(
            f"""
            INSERT INTO {self._tasks} (
                task_id, source_type, owner_user_id, status, objective, current_plan_id,
                current_plan_version, idempotency_key, checkpoint, last_error,
                lease_owner, lease_expires_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_id) DO UPDATE SET
                status = EXCLUDED.status,
                objective = EXCLUDED.objective,
                current_plan_id = EXCLUDED.current_plan_id,
                current_plan_version = EXCLUDED.current_plan_version,
                checkpoint = EXCLUDED.checkpoint,
                last_error = EXCLUDED.last_error,
                updated_at = EXCLUDED.updated_at
            """,
            (
                task.task_id,
                task.source_type,
                task.owner_user_id,
                task.status,
                task.objective,
                task.current_plan_id,
                task.current_plan_version,
                task.idempotency_key,
                _json(task.checkpoint),
                _json(task.last_error),
                task.lease_owner,
                task.lease_expires_at,
                task.created_at,
                task.updated_at,
            ),
        )

    # -- tasks ------------------------------------------------------------

    def create_task(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent] | None = None,
        outbox_events: list[OutboxEvent] | None = None,
    ) -> TaskRecord:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                if task.idempotency_key:
                    cursor.execute(
                        f"SELECT * FROM {self._tasks} WHERE idempotency_key = %s",
                        (task.idempotency_key,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        return self._task_model(existing)
                self._upsert_task(cursor, task)
                for event in events or []:
                    self._insert_event(cursor, event)
                for item in outbox_events or []:
                    self._insert_outbox(cursor, item)
        stored = self.get_task(task.task_id)
        assert stored is not None
        return stored

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(f"SELECT * FROM {self._tasks} WHERE task_id = %s", (task_id,))
                row = cursor.fetchone()
                return self._task_model(row) if row else None

    def find_task_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._tasks} WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                row = cursor.fetchone()
                return self._task_model(row) if row else None

    def commit(
        self,
        task: TaskRecord,
        *,
        events: list[TaskEvent] | None = None,
        outbox_events: list[OutboxEvent] | None = None,
    ) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                self._upsert_task(cursor, task)
                for event in events or []:
                    self._insert_event(cursor, event)
                for item in outbox_events or []:
                    self._insert_outbox(cursor, item)

    def list_unfinished_tasks(self, limit: int = 50) -> list[TaskRecord]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._tasks} "
                    "WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'unknown') "
                    "ORDER BY created_at LIMIT %s",
                    (limit,),
                )
                return [self._task_model(row) for row in cursor.fetchall()]

    def acquire_lease(self, task_id: str, owner: str, seconds: float) -> bool:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._tasks} SET lease_owner = %s, lease_expires_at = %s "
                    "WHERE task_id = %s AND (lease_owner IS NULL OR lease_expires_at < NOW() OR lease_owner = %s) "
                    "RETURNING task_id",
                    (owner, datetime.now(timezone.utc) + timedelta(seconds=seconds), task_id, owner),
                )
                return cursor.fetchone() is not None

    def release_lease(self, task_id: str, owner: str) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._tasks} SET lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE task_id = %s AND lease_owner = %s",
                    (task_id, owner),
                )

    # -- inputs -----------------------------------------------------------

    def add_input(self, item: TaskInput) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {self._inputs} (input_id, task_id, version, payload, created_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (task_id, version) DO UPDATE SET payload = EXCLUDED.payload",
                    (item.input_id, item.task_id, item.version, _json(item.payload), item.created_at),
                )

    def list_inputs(self, task_id: str) -> list[TaskInput]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._inputs} WHERE task_id = %s ORDER BY version",
                    (task_id,),
                )
                return [
                    TaskInput(
                        input_id=row["input_id"],
                        task_id=row["task_id"],
                        version=row["version"],
                        payload=row["payload"] or {},
                        created_at=row["created_at"],
                    )
                    for row in cursor.fetchall()
                ]

    # -- plans and steps --------------------------------------------------

    def save_plan(self, plan: Plan) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._plans} SET is_active = FALSE WHERE task_id = %s AND plan_id <> %s",
                    (plan.task_id, plan.plan_id),
                )
                cursor.execute(
                    f"""
                    INSERT INTO {self._plans} (plan_id, task_id, version, objective, status, is_active, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, TRUE, NOW(), NOW())
                    ON CONFLICT (plan_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        objective = EXCLUDED.objective,
                        is_active = TRUE,
                        updated_at = NOW()
                    """,
                    (plan.plan_id, plan.task_id, plan.version, plan.objective, plan.status),
                )
                for step in plan.steps:
                    self._upsert_step(cursor, plan.task_id, step)

    def _upsert_step(self, cursor, task_id: str, step: PlanStep) -> None:
        cursor.execute(
            f"""
            INSERT INTO {self._steps} (
                step_id, plan_id, task_id, step_order, capability, arguments, status,
                attempt_count, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, NOW(), NOW())
            ON CONFLICT (step_id) DO UPDATE SET
                status = EXCLUDED.status,
                arguments = EXCLUDED.arguments,
                capability = EXCLUDED.capability,
                updated_at = NOW()
            """,
            (step.step_id, step.plan_id, task_id, step.order, step.capability, _json(step.arguments), step.status),
        )

    def get_plan(self, plan_id: str) -> Plan | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(f"SELECT * FROM {self._plans} WHERE plan_id = %s", (plan_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                plan = self._plan_model(row)
                plan.steps = self.list_steps_with(cursor, plan_id)
                return plan

    def list_steps_with(self, cursor, plan_id: str) -> list[PlanStep]:
        cursor.execute(
            f"SELECT * FROM {self._steps} WHERE plan_id = %s ORDER BY step_order",
            (plan_id,),
        )
        return [self._step_model(row) for row in cursor.fetchall()]

    def get_active_plan(self, task_id: str) -> Plan | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._plans} WHERE task_id = %s AND is_active ORDER BY version DESC LIMIT 1",
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                plan = self._plan_model(row)
                plan.steps = self.list_steps_with(cursor, plan.plan_id)
                return plan

    def invalidate_plans(self, task_id: str, *, except_plan_id: str | None = None) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                if except_plan_id is None:
                    cursor.execute(f"UPDATE {self._plans} SET is_active = FALSE WHERE task_id = %s", (task_id,))
                else:
                    cursor.execute(
                        f"UPDATE {self._plans} SET is_active = FALSE WHERE task_id = %s AND plan_id <> %s",
                        (task_id, except_plan_id),
                    )

    def save_steps(self, task_id: str, steps: list[PlanStep]) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                for step in steps:
                    self._upsert_step(cursor, task_id, step)

    def update_step(
        self,
        step: PlanStep,
        *,
        attempt_count: int | None = None,
        approved_version: int | None = None,
        last_error: dict | None = None,
    ) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._steps} SET
                        status = %s,
                        arguments = %s,
                        attempt_count = COALESCE(%s, attempt_count),
                        approved_version = COALESCE(%s, approved_version),
                        last_error = COALESCE(%s, last_error),
                        updated_at = NOW()
                    WHERE step_id = %s
                    """,
                    (
                        step.status,
                        _json(step.arguments),
                        attempt_count,
                        approved_version,
                        _json(last_error),
                        step.step_id,
                    ),
                )

    def list_steps(self, plan_id: str) -> list[PlanStep]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                return self.list_steps_with(cursor, plan_id)

    def get_step(self, step_id: str) -> PlanStep | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(f"SELECT * FROM {self._steps} WHERE step_id = %s", (step_id,))
                row = cursor.fetchone()
                return self._step_model(row) if row else None

    # -- observations, calls, evidence ------------------------------------

    def save_observation(self, observation: Observation) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {self._observations} "
                    "(observation_id, task_id, plan_id, step_id, capability, status, output, error, evidence, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (observation_id) DO NOTHING",
                    (
                        observation.observation_id,
                        observation.task_id,
                        observation.plan_id,
                        observation.step_id,
                        observation.capability,
                        observation.status,
                        _json(observation.output),
                        _json(observation.error),
                        _json(observation.evidence or []),
                        observation.created_at,
                    ),
                )

    def list_observations(self, task_id: str) -> list[Observation]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._observations} WHERE task_id = %s ORDER BY created_at",
                    (task_id,),
                )
                return [self._observation_model(row) for row in cursor.fetchall()]

    def get_capability_call(self, idempotency_key: str) -> CapabilityCallRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._calls} WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                row = cursor.fetchone()
                return self._call_model(row) if row else None

    def save_capability_call(self, call: CapabilityCallRecord) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._calls} (
                        call_id, task_id, plan_id, step_id, capability, idempotency_key, request_id,
                        attempt, status, arguments, result, error, created_at, finished_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (call_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        result = EXCLUDED.result,
                        error = EXCLUDED.error,
                        finished_at = EXCLUDED.finished_at
                    """,
                    (
                        call.call_id,
                        call.task_id,
                        call.plan_id,
                        call.step_id,
                        call.capability,
                        call.idempotency_key,
                        call.request_id,
                        call.attempt,
                        call.status,
                        _json(call.arguments),
                        _json(call.result),
                        _json(call.error),
                        call.created_at,
                        call.finished_at,
                    ),
                )

    def save_evidence(self, evidence: EvidenceRecord) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {self._evidence} "
                    "(evidence_id, task_id, plan_id, step_id, observation_id, payload, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (evidence_id) DO NOTHING",
                    (
                        evidence.evidence_id,
                        evidence.task_id,
                        evidence.plan_id,
                        evidence.step_id,
                        evidence.observation_id,
                        _json(evidence.payload),
                        evidence.created_at,
                    ),
                )

    # -- approvals --------------------------------------------------------

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._approvals} (
                        approval_id, task_id, plan_id, step_id, capability, arguments, version,
                        status, reason, expires_at, decided_at, decided_by, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (approval_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        arguments = EXCLUDED.arguments,
                        reason = EXCLUDED.reason,
                        decided_at = EXCLUDED.decided_at,
                        decided_by = EXCLUDED.decided_by,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        approval.approval_id,
                        approval.task_id,
                        approval.plan_id,
                        approval.step_id,
                        approval.capability,
                        _json(approval.arguments),
                        approval.version,
                        approval.status,
                        approval.reason,
                        approval.expires_at,
                        approval.decided_at,
                        approval.decided_by,
                        approval.created_at,
                        approval.updated_at,
                    ),
                )

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(f"SELECT * FROM {self._approvals} WHERE approval_id = %s", (approval_id,))
                row = cursor.fetchone()
                return self._approval_model(row) if row else None

    def list_approvals(
        self, *, task_id: str | None = None, owner_user_id: str | None = None
    ) -> list[ApprovalRecord]:
        query = f"SELECT a.* FROM {self._approvals} a JOIN {self._tasks} t ON t.task_id = a.task_id WHERE TRUE"
        params: list[Any] = []
        if task_id is not None:
            query += " AND a.task_id = %s"
            params.append(task_id)
        if owner_user_id is not None:
            query += " AND t.owner_user_id = %s"
            params.append(owner_user_id)
        query += " ORDER BY a.created_at"
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(query, params)
                return [self._approval_model(row) for row in cursor.fetchall()]

    # -- events -----------------------------------------------------------

    def append_event(self, event: TaskEvent) -> TaskEvent:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                return self._insert_event(cursor, event)

    def list_events(self, task_id: str, *, after_sequence: int = 0) -> list[TaskEvent]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._events} WHERE task_id = %s AND sequence > %s ORDER BY sequence",
                    (task_id, after_sequence),
                )
                return [self._event_model(row) for row in cursor.fetchall()]

    # -- outbox -----------------------------------------------------------

    def enqueue_outbox(self, event: OutboxEvent) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                self._insert_outbox(cursor, event)

    def pending_outbox(self, limit: int = 50) -> list[OutboxEvent]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._outbox} WHERE status = 'pending' AND available_at <= NOW() "
                    "ORDER BY created_at LIMIT %s",
                    (limit,),
                )
                return [self._outbox_model(row) for row in cursor.fetchall()]

    def mark_outbox_sent(self, event_id: str) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._outbox} SET status = 'sent', published_at = NOW() WHERE event_id = %s",
                    (event_id,),
                )

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._outbox} SET attempt_count = attempt_count + 1, last_error = %s, "
                    "available_at = NOW() + (LEAST(POWER(2, attempt_count + 1), 30) * INTERVAL '1 second') "
                    "WHERE event_id = %s",
                    (error[:500], event_id),
                )
