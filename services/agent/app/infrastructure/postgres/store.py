"""PostgreSQL implementation of the AgentStore protocol.

PostgreSQL owns the authoritative Agent state. ``commit`` writes the Task
transition, its Task Events and its Outbox Events inside a single transaction,
which is what makes the Codex-style event stream crash safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
    TodoRecord,
)


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


class PostgresAgentStore:
    def __init__(self, pool: ConnectionPool, schema: str = "agent") -> None:
        self.pool = pool
        self.schema = schema
        self._person_fact_table_ready = False

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

    @property
    def _conversations(self) -> str:
        return f"{self.schema}.conversations"

    @property
    def _messages(self) -> str:
        return f"{self.schema}.messages"

    @property
    def _summary_jobs(self) -> str:
        return f"{self.schema}.conversation_summary_jobs"

    @property
    def _memories(self) -> str:
        return f"{self.schema}.memory_records"

    @property
    def _memory_sources(self) -> str:
        return f"{self.schema}.memory_sources"

    @property
    def _person_fact_snapshots(self) -> str:
        return f"{self.schema}.agent_person_fact_snapshots"

    def get_person_fact_snapshot(
        self,
        *,
        owner_user_id: str,
        person_key: str,
        snapshot_fingerprint: str,
    ) -> list[dict[str, Any]] | None:
        self._ensure_person_fact_table()
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT facts FROM {self._person_fact_snapshots}
                        WHERE owner_user_id=%s AND person_key=%s
                          AND snapshot_fingerprint=%s""",
                    (owner_user_id, person_key, snapshot_fingerprint),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                value = row[0]
                return value if isinstance(value, list) else None

    def save_person_fact_snapshot(
        self,
        *,
        owner_user_id: str,
        person_key: str,
        snapshot_fingerprint: str,
        facts: list[dict[str, Any]],
    ) -> None:
        self._ensure_person_fact_table()
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self._person_fact_snapshots}
                        (id,owner_user_id,person_key,snapshot_fingerprint,facts)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (owner_user_id,person_key,snapshot_fingerprint)
                        DO UPDATE SET facts=EXCLUDED.facts,updated_at=NOW()""",
                    (
                        str(uuid4()),
                        owner_user_id,
                        person_key,
                        snapshot_fingerprint,
                        Jsonb(facts),
                    ),
                )
            connection.commit()

    def _ensure_person_fact_table(self) -> None:
        if self._person_fact_table_ready:
            return
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._person_fact_snapshots} (
                        id UUID PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        person_key TEXT NOT NULL,
                        snapshot_fingerprint TEXT NOT NULL,
                        facts JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CONSTRAINT agent_person_fact_snapshots_uq
                            UNIQUE (owner_user_id, person_key, snapshot_fingerprint)
                    )"""
                )
                cursor.execute(
                    f"""CREATE INDEX IF NOT EXISTS agent_person_fact_snapshots_owner_idx
                        ON {self._person_fact_snapshots}
                        (owner_user_id, person_key, updated_at DESC)"""
                )
            connection.commit()
        self._person_fact_table_ready = True

    @staticmethod
    def _task_model(row: dict[str, Any]) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            source_type=row["source_type"],
            owner_user_id=row["owner_user_id"],
            status=row["status"],
            input=row["input"] or {},
            source_ref=row["source_ref"] or {},
            constraints=row["constraints"] or {},
            objective=row["objective"],
            current_plan_id=row["current_plan_id"],
            current_plan_version=row["current_plan_version"],
            idempotency_key=row["idempotency_key"],
            checkpoint=row["checkpoint"],
            understanding=row.get("understanding"),
            result=row.get("result"),
            replan_count=row.get("replan_count", 0),
            step_count=row.get("step_count", 0),
            model_call_count=row.get("model_call_count", 0),
            last_error=row["last_error"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            conversation_id=(
                str(row["conversation_id"]) if row.get("conversation_id") else None
            ),
            request_message_id=(
                str(row["request_message_id"]) if row.get("request_message_id") else None
            ),
            response_message_id=(
                str(row["response_message_id"]) if row.get("response_message_id") else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _conversation_model(row: dict[str, Any]) -> ConversationRecord:
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            owner_user_id=row["owner_user_id"],
            organization_id=row.get("organization_id"),
            title=row["title"],
            status=row["status"],
            source=row["source"],
            summary=row.get("summary"),
            summary_cursor=row.get("summary_cursor", 0),
            summary_until_message_id=(
                str(row["summary_until_message_id"])
                if row.get("summary_until_message_id")
                else None
            ),
            summary_version=row.get("summary_version", 0),
            summary_updated_at=row.get("summary_updated_at"),
            summary_method=row.get("summary_method") or "incremental",
            summary_token_count=row.get("summary_token_count", 0) or 0,
            last_message_at=row.get("last_message_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _message_model(row: dict[str, Any]) -> MessageRecord:
        return MessageRecord(
            message_id=str(row["message_id"]),
            conversation_id=str(row["conversation_id"]),
            role=row["role"],
            content=row["content"] or "",
            status=row["status"],
            task_id=row.get("task_id"),
            citations=row.get("citations") or [],
            client_message_id=row.get("client_message_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _summary_job_model(row: dict[str, Any]) -> ConversationSummaryJob:
        return ConversationSummaryJob(
            job_id=str(row["job_id"]),
            conversation_id=str(row["conversation_id"]),
            expected_summary_version=int(row["expected_summary_version"]),
            boundary_from_message_id=(
                str(row["boundary_from_message_id"])
                if row.get("boundary_from_message_id")
                else None
            ),
            boundary_to_message_id=str(row["boundary_to_message_id"]),
            status=row["status"],
            attempt_count=int(row.get("attempt_count") or 0),
            available_at=row["available_at"],
            lease_owner=row.get("lease_owner"),
            lease_until=row.get("lease_until"),
            last_error=row.get("last_error"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row.get("finished_at"),
        )

    @staticmethod
    def _memory_model(row: dict[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            owner_user_id=row["owner_user_id"],
            organization_id=row.get("organization_id"),
            memory_type=row["memory_type"],
            scope=row.get("scope") or "conversation",
            title=row["title"],
            content=row["content"],
            content_hash=row["content_hash"],
            memory_key=row["memory_key"],
            keywords=row.get("keywords") or [],
            source_conversation_id=str(row["source_conversation_id"]),
            source_message_ids=[
                str(item) for item in (row.get("source_message_ids") or [])
            ],
            extraction_method=row.get("extraction_method"),
            extraction_job_id=(
                str(row["extraction_job_id"])
                if row.get("extraction_job_id")
                else None
            ),
            confidence=float(row.get("confidence") or 0),
            importance=float(row.get("importance") or 0),
            access_count=int(row.get("access_count") or 0),
            last_accessed_at=row.get("last_accessed_at"),
            status=row["status"],
            superseded_by_memory_id=(
                str(row["superseded_by_memory_id"])
                if row.get("superseded_by_memory_id")
                else None
            ),
            expires_at=row.get("expires_at"),
            deleted_at=row.get("deleted_at"),
            embedding_model=row.get("embedding_model"),
            embedding_version=row.get("embedding_version"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _plan_model(row: dict[str, Any]) -> Plan:
        return Plan(
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            version=row["version"],
            parent_plan_id=row.get("parent_plan_id"),
            triggered_by_observation_id=row.get("triggered_by_observation_id"),
            replan_reason=row.get("replan_reason"),
            unsupported_intents=row.get("unsupported_intents") or [],
            warnings=row.get("warnings") or [],
            requires_user_confirmation=bool(
                row.get("requires_user_confirmation", False)
            ),
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
            depends_on=row.get("depends_on"),
            status=row["status"],
            attempt_count=int(row.get("attempt_count") or 0),
            replaced_by_step_id=row.get("replaced_by_step_id"),
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
            arguments_hash=row.get("arguments_hash"),
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

    def _insert_input(self, cursor, item: TaskInput) -> None:
        cursor.execute(
            f"INSERT INTO {self._inputs} "
            "(input_id, task_id, version, payload, created_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (task_id, version) DO UPDATE SET payload = EXCLUDED.payload",
            (
                item.input_id,
                item.task_id,
                item.version,
                _json(item.payload),
                item.created_at,
            ),
        )

    def _insert_conversation(
        self, cursor, conversation: ConversationRecord
    ) -> ConversationRecord:
        cursor.execute(
            f"""INSERT INTO {self._conversations} (
                    conversation_id, owner_user_id, organization_id, title,
                    status, source, summary, summary_cursor,
                    summary_until_message_id, summary_version, summary_updated_at,
                    summary_method, summary_token_count, last_message_at,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (conversation_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    summary_cursor = EXCLUDED.summary_cursor,
                    summary_until_message_id = EXCLUDED.summary_until_message_id,
                    summary_version = EXCLUDED.summary_version,
                    summary_updated_at = EXCLUDED.summary_updated_at,
                    summary_method = EXCLUDED.summary_method,
                    summary_token_count = EXCLUDED.summary_token_count,
                    last_message_at = EXCLUDED.last_message_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING *""",
            (
                conversation.conversation_id,
                conversation.owner_user_id,
                conversation.organization_id,
                conversation.title,
                conversation.status,
                conversation.source,
                conversation.summary,
                conversation.summary_cursor,
                conversation.summary_until_message_id,
                conversation.summary_version,
                conversation.summary_updated_at,
                conversation.summary_method,
                conversation.summary_token_count,
                conversation.last_message_at,
                conversation.created_at,
                conversation.updated_at,
            ),
        )
        row = cursor.fetchone()
        return self._conversation_model(row)

    def _insert_message(self, cursor, message: MessageRecord) -> MessageRecord:
        cursor.execute(
            f"""INSERT INTO {self._messages} (
                    message_id, conversation_id, role, content, status,
                    task_id, citations, client_message_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *""",
            (
                message.message_id,
                message.conversation_id,
                message.role,
                message.content,
                message.status,
                message.task_id,
                _json(message.citations),
                message.client_message_id,
                message.created_at,
                message.updated_at,
            ),
        )
        row = cursor.fetchone()
        cursor.execute(
            f"""UPDATE {self._conversations}
                SET last_message_at = %s, updated_at = %s
                WHERE conversation_id = %s""",
            (message.created_at, message.created_at, message.conversation_id),
        )
        return self._message_model(row)

    def _upsert_task(self, cursor, task: TaskRecord) -> None:
        cursor.execute(
            f"""
            INSERT INTO {self._tasks} (
                task_id, source_type, owner_user_id, status, objective, current_plan_id,
                current_plan_version, idempotency_key, checkpoint, understanding, result,
                replan_count, step_count, model_call_count, last_error,
                lease_owner, lease_expires_at, input, source_ref, constraints,
                conversation_id, request_message_id, response_message_id,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (task_id) DO UPDATE SET
                status = EXCLUDED.status,
                objective = EXCLUDED.objective,
                current_plan_id = EXCLUDED.current_plan_id,
                current_plan_version = EXCLUDED.current_plan_version,
                checkpoint = EXCLUDED.checkpoint,
                understanding = EXCLUDED.understanding,
                result = EXCLUDED.result,
                replan_count = EXCLUDED.replan_count,
                step_count = EXCLUDED.step_count,
                model_call_count = EXCLUDED.model_call_count,
                last_error = EXCLUDED.last_error,
                input = EXCLUDED.input,
                source_ref = EXCLUDED.source_ref,
                constraints = EXCLUDED.constraints,
                conversation_id = EXCLUDED.conversation_id,
                request_message_id = EXCLUDED.request_message_id,
                response_message_id = EXCLUDED.response_message_id,
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
                _json(task.understanding),
                _json(task.result),
                task.replan_count,
                task.step_count,
                task.model_call_count,
                _json(task.last_error),
                task.lease_owner,
                task.lease_expires_at,
                _json(task.input),
                _json(task.source_ref),
                _json(task.constraints),
                task.conversation_id,
                task.request_message_id,
                task.response_message_id,
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
        inputs: list[TaskInput] | None = None,
        conversation: ConversationRecord | None = None,
        messages: list[MessageRecord] | None = None,
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
                if conversation is not None:
                    self._insert_conversation(cursor, conversation)
                self._upsert_task(cursor, task)
                for item in inputs or []:
                    self._insert_input(cursor, item)
                for message in messages or []:
                    self._insert_message(cursor, message)
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

    def list_tasks_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        wanted = [status for status in (statuses or []) if status]
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                if wanted:
                    cursor.execute(
                        f"SELECT * FROM {self._tasks} WHERE owner_user_id = %s AND status = ANY(%s) "
                        "ORDER BY created_at DESC LIMIT %s",
                        (owner_user_id, wanted, max(0, limit)),
                    )
                else:
                    cursor.execute(
                        f"SELECT * FROM {self._tasks} WHERE owner_user_id = %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (owner_user_id, max(0, limit)),
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

    def renew_lease(self, task_id: str, owner: str, seconds: float) -> bool:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._tasks} SET lease_expires_at = %s "
                    "WHERE task_id = %s AND lease_owner = %s RETURNING task_id",
                    (
                        datetime.now(timezone.utc) + timedelta(seconds=seconds),
                        task_id,
                        owner,
                    ),
                )
                return cursor.fetchone() is not None

    # -- inputs -----------------------------------------------------------

    def add_input(self, item: TaskInput) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                self._insert_input(cursor, item)

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
                    INSERT INTO {self._plans} (
                        plan_id, task_id, version, parent_plan_id,
                        triggered_by_observation_id, replan_reason,
                        unsupported_intents, warnings, requires_user_confirmation,
                        objective, status, is_active, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW(), NOW())
                    ON CONFLICT (plan_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        objective = EXCLUDED.objective,
                        replan_reason = EXCLUDED.replan_reason,
                        unsupported_intents = EXCLUDED.unsupported_intents,
                        warnings = EXCLUDED.warnings,
                        requires_user_confirmation = EXCLUDED.requires_user_confirmation,
                        is_active = TRUE,
                        updated_at = NOW()
                    """,
                    (
                        plan.plan_id,
                        plan.task_id,
                        plan.version,
                        plan.parent_plan_id,
                        plan.triggered_by_observation_id,
                        plan.replan_reason,
                        _json(plan.unsupported_intents),
                        _json(plan.warnings),
                        plan.requires_user_confirmation,
                        plan.objective,
                        plan.status,
                    ),
                )
                for step in plan.steps:
                    self._upsert_step(cursor, plan.task_id, step)

    def _upsert_step(self, cursor, task_id: str, step: PlanStep) -> None:
        cursor.execute(
            f"""
            INSERT INTO {self._steps} (
                step_id, plan_id, task_id, step_order, capability, arguments, status,
                depends_on, attempt_count, replaced_by_step_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (step_id) DO UPDATE SET
                status = EXCLUDED.status,
                arguments = EXCLUDED.arguments,
                capability = EXCLUDED.capability,
                depends_on = EXCLUDED.depends_on,
                attempt_count = EXCLUDED.attempt_count,
                replaced_by_step_id = EXCLUDED.replaced_by_step_id,
                updated_at = NOW()
            """,
            (
                step.step_id,
                step.plan_id,
                task_id,
                step.order,
                step.capability,
                _json(step.arguments),
                step.status,
                _json(step.depends_on) if step.depends_on is not None else None,
                step.attempt_count,
                step.replaced_by_step_id,
            ),
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

    def next_plan_version(self, task_id: str) -> int:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COALESCE(MAX(version), 0) + 1 FROM {self._plans} WHERE task_id = %s",
                    (task_id,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row is not None else 1

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
                        depends_on = %s,
                        attempt_count = COALESCE(%s, attempt_count),
                        approved_version = COALESCE(%s, approved_version),
                        last_error = COALESCE(%s, last_error),
                        replaced_by_step_id = COALESCE(%s, replaced_by_step_id),
                        updated_at = NOW()
                    WHERE step_id = %s
                    """,
                    (
                        step.status,
                        _json(step.arguments),
                        _json(step.depends_on) if step.depends_on is not None else None,
                        attempt_count,
                        approved_version,
                        _json(last_error),
                        step.replaced_by_step_id,
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

    def find_capability_call_for_step(
        self, task_id: str, step_id: str
    ) -> CapabilityCallRecord | None:
        """Latest call recorded for one Step.

        Used to resume a Step whose worker died mid-call: the persisted row, not
        an in-memory attempt counter, tells whether the external call happened.
        """

        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._calls} WHERE task_id = %s AND step_id = %s "
                    "ORDER BY attempt DESC, created_at DESC LIMIT 1",
                    (task_id, step_id),
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
                        attempt = EXCLUDED.attempt,
                        arguments = EXCLUDED.arguments,
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
                        approval_id, task_id, plan_id, step_id, capability, arguments, arguments_hash, version,
                        status, reason, expires_at, decided_at, decided_by, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (approval_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        arguments = EXCLUDED.arguments,
                        arguments_hash = EXCLUDED.arguments_hash,
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
                        approval.arguments_hash,
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

    def claim_outbox(
        self, limit: int = 50, *, lease_seconds: float = 60.0
    ) -> list[OutboxEvent]:
        """Claim deliverable rows without letting sibling workers publish them."""

        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""
                    WITH claimable AS (
                        SELECT event_id
                        FROM {self._outbox}
                        WHERE status IN ('pending', 'publishing')
                          AND available_at <= NOW()
                        ORDER BY created_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE {self._outbox} AS outbox
                    SET status = 'publishing',
                        available_at = NOW() + make_interval(secs => %s)
                    FROM claimable
                    WHERE outbox.event_id = claimable.event_id
                    RETURNING outbox.*
                    """,
                    (max(1, int(limit)), max(1.0, float(lease_seconds))),
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
                    f"UPDATE {self._outbox} SET status = 'pending', attempt_count = attempt_count + 1, last_error = %s, "
                    "available_at = NOW() + (LEAST(POWER(2, attempt_count + 1), 30) * INTERVAL '1 second') "
                    "WHERE event_id = %s",
                    (error[:500], event_id),
                )

    # -- conversation history ---------------------------------------------

    def create_conversation(
        self, conversation: ConversationRecord
    ) -> ConversationRecord:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                stored = self._insert_conversation(cursor, conversation)
            connection.commit()
        return stored

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._conversations} WHERE conversation_id = %s",
                    (conversation_id,),
                )
                row = cursor.fetchone()
        return self._conversation_model(row) if row else None

    def save_conversation(self, conversation: ConversationRecord) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self._conversations} SET
                            title = %s, status = %s, summary = %s,
                            summary_cursor = %s, summary_until_message_id = %s,
                            summary_version = %s, summary_updated_at = %s,
                            summary_method = %s, summary_token_count = %s,
                            last_message_at = %s, updated_at = %s
                        WHERE conversation_id = %s""",
                    (
                        conversation.title,
                        conversation.status,
                        conversation.summary,
                        conversation.summary_cursor,
                        conversation.summary_until_message_id,
                        conversation.summary_version,
                        conversation.summary_updated_at,
                        conversation.summary_method,
                        conversation.summary_token_count,
                        conversation.last_message_at,
                        conversation.updated_at,
                        conversation.conversation_id,
                    ),
                )
            connection.commit()

    def list_conversations_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationRecord]:
        sql = f"SELECT * FROM {self._conversations} WHERE owner_user_id = %s"
        params: list[Any] = [owner_user_id]
        if statuses:
            sql += " AND status = ANY(%s)"
            params.append(list(statuses))
        sql += (
            " ORDER BY COALESCE(last_message_at, created_at) DESC"
            " LIMIT %s OFFSET %s"
        )
        params.append(max(int(limit), 1))
        params.append(max(int(offset), 0))
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
        return [self._conversation_model(row) for row in rows]

    def count_conversations_for_owner(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
    ) -> int:
        sql = f"SELECT COUNT(*) FROM {self._conversations} WHERE owner_user_id = %s"
        params: list[Any] = [owner_user_id]
        if statuses:
            sql += " AND status = ANY(%s)"
            params.append(list(statuses))
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def delete_conversation(
        self, conversation_id: str, *, owner_user_id: str
    ) -> bool:
        # Messages are removed by the ON DELETE CASCADE foreign key.
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self._conversations} "
                    "WHERE conversation_id = %s AND owner_user_id = %s",
                    (conversation_id, owner_user_id),
                )
                deleted = cursor.rowcount > 0
            connection.commit()
        return deleted

    def add_message(self, message: MessageRecord) -> MessageRecord:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                stored = self._insert_message(cursor, message)
            connection.commit()
        return stored

    def get_message(self, message_id: str) -> MessageRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._messages} WHERE message_id = %s",
                    (message_id,),
                )
                row = cursor.fetchone()
        return self._message_model(row) if row else None

    def update_message(self, message: MessageRecord) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self._messages} SET
                            content = %s, status = %s, citations = %s, updated_at = %s
                        WHERE message_id = %s""",
                    (
                        message.content,
                        message.status,
                        _json(message.citations),
                        message.updated_at,
                        message.message_id,
                    ),
                )
            connection.commit()

    def list_messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[MessageRecord]:
        sql = (
            f"SELECT * FROM {self._messages} WHERE conversation_id = %s"
            " ORDER BY created_at"
        )
        params: list[Any] = [conversation_id]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(max(int(limit), 1))
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
        return [self._message_model(row) for row in rows]

    def count_messages(self, conversation_id: str) -> int:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {self._messages} WHERE conversation_id = %s",
                    (conversation_id,),
                )
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def count_messages_by_conversation(
        self, conversation_ids: list[str]
    ) -> dict[str, int]:
        ids = [str(item) for item in conversation_ids if item]
        if not ids:
            return {}
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT conversation_id::text AS conversation_id, "
                    f"COUNT(*) AS message_count FROM {self._messages} "
                    "WHERE conversation_id = ANY(%s::uuid[]) "
                    "GROUP BY conversation_id",
                    (ids,),
                )
                rows = cursor.fetchall()
        return {
            str(row["conversation_id"]): int(row["message_count"]) for row in rows
        }

    def list_completed_messages_after_boundary(
        self,
        conversation_id: str,
        *,
        boundary_message_id: str | None = None,
        boundary_to_message_id: str | None = None,
        exclude_task_id: str | None = None,
        limit: int | None = None,
    ) -> list[MessageRecord]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                boundary = None
                if boundary_message_id:
                    cursor.execute(
                        f"SELECT message_id, created_at FROM {self._messages} "
                        "WHERE message_id = %s",
                        (boundary_message_id,),
                    )
                    boundary = cursor.fetchone()
                    if boundary is None:
                        return []
                boundary_to = None
                if boundary_to_message_id:
                    cursor.execute(
                        f"SELECT message_id, created_at FROM {self._messages} "
                        "WHERE message_id = %s",
                        (boundary_to_message_id,),
                    )
                    boundary_to = cursor.fetchone()
                    if boundary_to is None:
                        return []

                sql = (
                    f"SELECT * FROM {self._messages} "
                    "WHERE conversation_id = %s AND status = 'completed'"
                )
                params: list[Any] = [conversation_id]
                if exclude_task_id is not None:
                    sql += " AND COALESCE(task_id, '') <> %s"
                    params.append(exclude_task_id)
                if boundary is not None:
                    sql += " AND (created_at, message_id) > (%s, %s)"
                    params.extend([boundary["created_at"], boundary["message_id"]])
                if boundary_to is not None:
                    sql += " AND (created_at, message_id) <= (%s, %s)"
                    params.extend(
                        [boundary_to["created_at"], boundary_to["message_id"]]
                    )
                sql += " ORDER BY created_at, message_id"
                if limit is not None:
                    sql += " LIMIT %s"
                    params.append(max(0, int(limit)))
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
        return [self._message_model(row) for row in rows]

    def create_conversation_summary_job(
        self, job: ConversationSummaryJob
    ) -> ConversationSummaryJob:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""INSERT INTO {self._summary_jobs} (
                            job_id, conversation_id, expected_summary_version,
                            boundary_from_message_id, boundary_to_message_id,
                            status, attempt_count, available_at, lease_owner,
                            lease_until, last_error, created_at, updated_at, finished_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (
                            conversation_id, expected_summary_version, boundary_to_message_id
                        ) DO NOTHING
                        RETURNING *""",
                    (
                        job.job_id,
                        job.conversation_id,
                        job.expected_summary_version,
                        job.boundary_from_message_id,
                        job.boundary_to_message_id,
                        job.status,
                        job.attempt_count,
                        job.available_at,
                        job.lease_owner,
                        job.lease_until,
                        job.last_error,
                        job.created_at,
                        job.updated_at,
                        job.finished_at,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"""SELECT * FROM {self._summary_jobs}
                            WHERE conversation_id = %s
                              AND expected_summary_version = %s
                              AND boundary_to_message_id = %s""",
                        (
                            job.conversation_id,
                            job.expected_summary_version,
                            job.boundary_to_message_id,
                        ),
                    )
                    row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("conversation summary job was not persisted")
        return self._summary_job_model(row)

    def claim_conversation_summary_jobs(
        self,
        *,
        owner: str,
        limit: int = 10,
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> list[ConversationSummaryJob]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""WITH claimable AS (
                            SELECT job_id
                            FROM {self._summary_jobs}
                            WHERE status IN ('pending', 'failed')
                              AND available_at <= NOW()
                              AND attempt_count < %s
                              AND (
                                  lease_until IS NULL
                                  OR lease_until <= NOW()
                                  OR lease_owner = %s
                              )
                            ORDER BY created_at
                            LIMIT %s
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE {self._summary_jobs} AS jobs
                        SET status = 'running',
                            lease_owner = %s,
                            lease_until = NOW() + make_interval(secs => %s),
                            updated_at = NOW()
                        FROM claimable
                        WHERE jobs.job_id = claimable.job_id
                        RETURNING jobs.*""",
                    (
                        max(1, int(max_attempts)),
                        owner,
                        max(1, int(limit)),
                        owner,
                        max(1.0, float(lease_seconds)),
                    ),
                )
                rows = cursor.fetchall()
            connection.commit()
        return [self._summary_job_model(row) for row in rows]

    def complete_conversation_summary_job(
        self, job_id: str, *, owner: str, finished_at: datetime
    ) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self._summary_jobs}
                        SET status = 'succeeded', lease_owner = NULL,
                            lease_until = NULL, finished_at = %s,
                            updated_at = %s, last_error = NULL
                        WHERE job_id = %s AND lease_owner = %s""",
                    (finished_at, finished_at, job_id, owner),
                )
            connection.commit()

    def fail_conversation_summary_job(
        self,
        job_id: str,
        *,
        owner: str,
        error: str,
        available_at: datetime,
    ) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self._summary_jobs}
                        SET status = 'failed',
                            attempt_count = attempt_count + 1,
                            last_error = %s,
                            available_at = %s,
                            lease_owner = NULL,
                            lease_until = NULL,
                            updated_at = NOW()
                        WHERE job_id = %s AND lease_owner = %s""",
                    (error[:1000], available_at, job_id, owner),
                )
            connection.commit()

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
        del boundary_from_message_id
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self._conversations}
                        SET summary = %s,
                            summary_cursor = summary_cursor + 1,
                            summary_until_message_id = %s,
                            summary_version = summary_version + 1,
                            summary_updated_at = %s,
                            summary_method = %s,
                            summary_token_count = %s,
                            updated_at = %s
                        WHERE conversation_id = %s
                          AND summary_version = %s
                        RETURNING conversation_id""",
                    (
                        summary,
                        boundary_to_message_id,
                        updated_at,
                        summary_method,
                        max(0, int(summary_token_count)),
                        updated_at,
                        conversation_id,
                        expected_version,
                    ),
                )
                updated = cursor.fetchone() is not None
            connection.commit()
        return updated

    def create_memory(self, memory: MemoryRecord) -> MemoryRecord:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"""INSERT INTO {self._memories} (
                            memory_id, owner_user_id, organization_id, memory_type,
                            scope, title, content, content_hash, memory_key, keywords,
                            source_conversation_id, source_message_ids, extraction_method,
                            extraction_job_id, confidence, importance, access_count,
                            last_accessed_at, status, superseded_by_memory_id, expires_at,
                            deleted_at, embedding_model, embedding_version,
                            created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::text[],
                            %s, %s::uuid[], %s, %s::uuid, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (
                            owner_user_id, source_conversation_id, memory_type, memory_key
                        ) WHERE status = 'active'
                        DO NOTHING
                        RETURNING *""",
                    (
                        memory.memory_id,
                        memory.owner_user_id,
                        memory.organization_id,
                        memory.memory_type,
                        memory.scope,
                        memory.title,
                        memory.content,
                        memory.content_hash,
                        memory.memory_key,
                        memory.keywords,
                        memory.source_conversation_id,
                        memory.source_message_ids,
                        memory.extraction_method,
                        memory.extraction_job_id,
                        memory.confidence,
                        memory.importance,
                        memory.access_count,
                        memory.last_accessed_at,
                        memory.status,
                        memory.superseded_by_memory_id,
                        memory.expires_at,
                        memory.deleted_at,
                        memory.embedding_model,
                        memory.embedding_version,
                        memory.created_at,
                        memory.updated_at,
                    ),
                )
                row = cursor.fetchone()
                if row is None and memory.status == "active":
                    cursor.execute(
                        f"""SELECT * FROM {self._memories}
                            WHERE owner_user_id = %s
                              AND source_conversation_id = %s
                              AND memory_type = %s
                              AND memory_key = %s
                              AND status = 'active'""",
                        (
                            memory.owner_user_id,
                            memory.source_conversation_id,
                            memory.memory_type,
                            memory.memory_key,
                        ),
                    )
                    row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("memory was not persisted")
        return self._memory_model(row)

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._memories} WHERE memory_id = %s",
                    (memory_id,),
                )
                row = cursor.fetchone()
        return self._memory_model(row) if row else None

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
        sql = (
            f"SELECT * FROM {self._memories} "
            "WHERE owner_user_id = %s AND source_conversation_id = %s"
        )
        params: list[Any] = [owner_user_id, conversation_id]
        if statuses:
            sql += " AND status = ANY(%s)"
            params.append(list(statuses))
        if memory_types:
            sql += " AND memory_type = ANY(%s)"
            params.append(list(memory_types))
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT %s OFFSET %s"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
        return [self._memory_model(row) for row in rows]

    def count_memories(
        self,
        owner_user_id: str,
        *,
        conversation_id: str,
        statuses: list[str] | None = None,
        memory_types: list[str] | None = None,
    ) -> int:
        sql = (
            f"SELECT COUNT(*) FROM {self._memories} "
            "WHERE owner_user_id = %s AND source_conversation_id = %s"
        )
        params: list[Any] = [owner_user_id, conversation_id]
        if statuses:
            sql += " AND status = ANY(%s)"
            params.append(list(statuses))
        if memory_types:
            sql += " AND memory_type = ANY(%s)"
            params.append(list(memory_types))
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def search_memories(
        self,
        owner_user_id: str,
        *,
        conversation_id: str,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        normalized = str(query or "").strip()
        sql = (
            f"SELECT * FROM {self._memories} "
            "WHERE owner_user_id = %s AND source_conversation_id = %s "
            "AND status = 'active'"
        )
        params: list[Any] = [owner_user_id, conversation_id]
        if normalized:
            conditions = [
                "content ILIKE %s",
                "title ILIKE %s",
                "to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)",
            ]
            pattern = f"%{normalized}%"
            params.extend([pattern, pattern, normalized])
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM unnest(keywords) AS memory_keyword "
                "WHERE strpos(lower(%s), lower(memory_keyword)) > 0"
                ")"
            )
            params.append(normalized)
            sql += " AND (" + " OR ".join(conditions) + ")"
        sql += " ORDER BY importance DESC, confidence DESC, updated_at DESC LIMIT %s"
        params.append(max(1, int(limit)))
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
        return [self._memory_model(row) for row in rows]

    def delete_memory(
        self,
        memory_id: str,
        *,
        owner_user_id: str,
        conversation_id: str,
    ) -> bool:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""DELETE FROM {self._memories}
                        WHERE memory_id = %s
                          AND owner_user_id = %s
                          AND source_conversation_id = %s""",
                    (memory_id, owner_user_id, conversation_id),
                )
                deleted = cursor.rowcount > 0
            connection.commit()
        return deleted

    def add_memory_sources(self, sources: list[MemorySourceRecord]) -> None:
        if not sources:
            return
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                for source in sources:
                    cursor.execute(
                        f"""INSERT INTO {self._memory_sources} (memory_id, message_id)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING""",
                        (source.memory_id, source.message_id),
                    )
            connection.commit()

    def list_memory_sources(self, memory_id: str) -> list[MemorySourceRecord]:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT memory_id::text, message_id::text
                        FROM {self._memory_sources}
                        WHERE memory_id = %s
                        ORDER BY message_id""",
                    (memory_id,),
                )
                rows = cursor.fetchall()
        return [
            MemorySourceRecord(memory_id=row[0], message_id=row[1]) for row in rows
        ]


class PostgresTodoStore:
    """PostgreSQL implementation of the TodoStore protocol.

    The table is {schema}.agent_todos (see the Agent todo-ledger migration).
    It is intentionally independent of the runtime tables: the execution kernel
    never reads a to-do, and the desktop never reads a Plan.
    """

    # -- the desktop may edit exactly these columns -------------------------
    MUTABLE_COLUMNS = (
        "title",
        "due_at",
        "due_expression",
        "timezone",
        "notes",
        "status",
    )

    def __init__(self, pool: ConnectionPool, schema: str = "agent") -> None:
        self.pool = pool
        self.schema = schema

    @property
    def _todos(self) -> str:
        return f"{self.schema}.agent_todos"

    @staticmethod
    def _todo_model(row: dict[str, Any]) -> TodoRecord:
        return TodoRecord(
            todo_id=row["todo_id"],
            owner_user_id=row["owner_user_id"],
            title=row["title"],
            due_at=row["due_at"],
            due_expression=row["due_expression"],
            timezone=row["timezone"],
            notes=row["notes"],
            status=row["status"],
            source=row["source"] or {},
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def create_todo(self, todo: TodoRecord) -> TodoRecord:
        sql = f"""
            INSERT INTO {self._todos} (
                todo_id, owner_user_id, title, due_at, due_expression,
                timezone, notes, status, source, plan_id, step_id,
                idempotency_key, created_at, updated_at, completed_at
            ) VALUES (
                %(todo_id)s, %(owner_user_id)s, %(title)s, %(due_at)s, %(due_expression)s,
                %(timezone)s, %(notes)s, %(status)s, %(source)s, %(plan_id)s, %(step_id)s,
                %(idempotency_key)s, %(created_at)s, %(updated_at)s, %(completed_at)s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
        """
        params = {
            "todo_id": todo.todo_id,
            "owner_user_id": todo.owner_user_id,
            "title": todo.title,
            "due_at": todo.due_at,
            "due_expression": todo.due_expression,
            "timezone": todo.timezone,
            "notes": todo.notes,
            "status": todo.status,
            "source": _json(todo.source),
            "plan_id": todo.plan_id,
            "step_id": todo.step_id,
            "idempotency_key": todo.idempotency_key,
            "created_at": todo.created_at,
            "updated_at": todo.updated_at,
            "completed_at": todo.completed_at,
        }
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, params)
                row = cursor.fetchone()
            connection.commit()
        if row is not None:
            return self._todo_model(row)
        # The idempotency key already existed: the retried Step reuses that row.
        existing = self.find_todo_by_idempotency_key(todo.idempotency_key or "")
        return existing if existing is not None else todo

    def get_todo(self, todo_id: str) -> TodoRecord | None:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._todos} WHERE todo_id = %s", (todo_id,)
                )
                row = cursor.fetchone()
        return self._todo_model(row) if row else None

    def find_todo_by_idempotency_key(self, idempotency_key: str) -> TodoRecord | None:
        if not idempotency_key:
            return None
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"SELECT * FROM {self._todos} WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        return self._todo_model(row) if row else None

    def list_todos(
        self,
        owner_user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[TodoRecord]:
        sql = f"SELECT * FROM {self._todos} WHERE owner_user_id = %s"
        params: list[Any] = [owner_user_id]
        if statuses:
            sql += " AND status = ANY(%s)"
            params.append(list(statuses))
        # Overdue rows are deliberately not filtered out: an unfinished to-do
        # keeps showing on the desktop until the owner deletes it.
        sql += " ORDER BY due_at ASC NULLS LAST, created_at ASC LIMIT %s"
        params.append(max(0, int(limit)))
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [self._todo_model(row) for row in rows]

    def update_todo(
        self,
        todo_id: str,
        *,
        owner_user_id: str,
        changes: dict,
    ) -> TodoRecord | None:
        assignments: list[str] = []
        params: dict[str, Any] = {
            "todo_id": todo_id,
            "owner_user_id": owner_user_id,
            "updated_at": datetime.now(timezone.utc),
        }
        for column in self.MUTABLE_COLUMNS:
            if column not in changes:
                continue
            assignments.append(f"{column} = %({column})s")
            params[column] = _json(changes[column]) if column == "source" else changes[column]
        if not assignments:
            return self.get_todo(todo_id)
        assignments.append("updated_at = %(updated_at)s")
        assignments.append(
            "completed_at = CASE WHEN %(status_expr)s = 'done' "
            "THEN COALESCE(completed_at, %(updated_at)s) ELSE NULL END"
        )
        params["status_expr"] = changes.get("status", "")
        sql = (
            f"UPDATE {self._todos} SET {', '.join(assignments)} "
            "WHERE todo_id = %(todo_id)s AND owner_user_id = %(owner_user_id)s "
            "RETURNING *"
        )
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, params)
                row = cursor.fetchone()
            connection.commit()
        return self._todo_model(row) if row else None

    def delete_todo(self, todo_id: str, *, owner_user_id: str) -> bool:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self._todos} "
                    "WHERE todo_id = %s AND owner_user_id = %s",
                    (todo_id, owner_user_id),
                )
                deleted = cursor.rowcount > 0
            connection.commit()
        return deleted
