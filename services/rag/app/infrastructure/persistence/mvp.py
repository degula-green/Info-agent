from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app.config import settings
from app.domain.rag import (
    Candidate,
    Chunk,
    Entity,
    EntityAlias,
    EntityMount,
    ResourceContext,
    stable_id,
)
from app.domain.state import (
    ProjectionStatus,
    validate_job_transition,
    validate_projection_transition,
)


ZERO_UUID = "00000000-0000-0000-0000-000000000000"


class DatabaseUnavailable(RuntimeError):
    pass


def payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_uuid() -> str:
    return str(uuid.uuid4())


def _safe_schema(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("RAG_DATABASE_SCHEMA must be a simple SQL identifier")
    if not value.startswith("rag"):
        raise ValueError("MVP repository requires a rag-owned schema")
    return value


def _uuid_or_none(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value)


def _vector_literal(values: list[float]) -> str:
    """pgvector takes a bracketed literal; passing text avoids needing a
    registered adapter for the vector type."""
    return "[" + ",".join(f"{float(value):.8g}" for value in values) + "]"


def _similarity_ratio(left: str, right: str) -> float:
    """String ratio for the in-memory fuzzy locator.

    Postgres uses pg_trgm; the test double only has to be close enough to
    exercise the locator's layer ordering without a database.
    """
    if not left or not right:
        return 0.0
    import difflib

    return difflib.SequenceMatcher(None, left, right).ratio()


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _as_json(value: Any) -> str:
    return json.dumps({} if value is None else value, ensure_ascii=False)


def _resolve_job_scope(
    payload: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[str, str]:
    audience = str(payload.get("source_audience_policy") or "")
    scope_type = str(payload.get("scope_type") or "").strip()
    scope_id = str(payload.get("scope_id") or "").strip()
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    organization_id = str(payload.get("organization_id") or "").strip()

    if scope_type or scope_id:
        if scope_type not in {"user", "organization"}:
            raise ValueError("knowledge.ready scope_type must be user or organization")
        if not scope_id or scope_id == ZERO_UUID:
            raise ValueError("knowledge.ready scope_id is required")
        try:
            uuid.UUID(scope_id)
        except ValueError as exc:
            raise ValueError("knowledge.ready scope_id must be a UUID") from exc
        if scope_type == "user":
            if owner_user_id and owner_user_id != scope_id:
                raise ValueError("knowledge.ready owner_user_id mismatches scope_id")
            if audience == "owner_only" and not owner_user_id:
                raise ValueError("owner_only event is missing owner_user_id")
        if scope_type == "organization":
            if organization_id and organization_id != scope_id:
                raise ValueError(
                    "knowledge.ready organization_id mismatches scope_id"
                )
            if not organization_id:
                raise ValueError(
                    "organization event is missing organization_id"
                )
        return scope_type, scope_id

    if audience in {"organization_members", "source_conversation_members"}:
        legacy_scope_id = str(envelope.get("organization_id") or "").strip()
        if not legacy_scope_id or legacy_scope_id == ZERO_UUID:
            raise ValueError("organization event is missing organization_id")
        return "organization", legacy_scope_id
    if audience == "owner_only":
        if not owner_user_id or owner_user_id == ZERO_UUID:
            raise ValueError("owner_only event is missing owner_user_id")
        return "user", owner_user_id
    raise ValueError("knowledge.ready scope is not authoritative")


class PostgresRagMVPRepository:
    def __init__(self, *, connection_factory: Any | None = None) -> None:
        self.schema = _safe_schema(settings.database_schema)
        self._connection_factory = connection_factory
        self._pool: Any | None = None
        self._source_payload_ready = False

    @property
    def configured(self) -> bool:
        return bool(settings.database_url) or self._connection_factory is not None

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._connection_factory is not None:
            connection = self._connection_factory()
        else:
            if not settings.database_url:
                raise DatabaseUnavailable("RAG_DATABASE_URL is not configured")
            from psycopg_pool import ConnectionPool

            if self._pool is None:
                self._pool = ConnectionPool(
                    conninfo=settings.database_url,
                    min_size=max(1, settings.database_min_pool_size),
                    max_size=max(settings.database_min_pool_size, settings.database_max_pool_size),
                    # The database is remote in local development, so TCP
                    # connections can be dropped while idle. Validate a pooled
                    # connection before handing it to a request instead of
                    # surfacing a transient 503 to the Agent.
                    check=ConnectionPool.check_connection,
                    kwargs={
                        "connect_timeout": max(1, int(settings.database_connect_timeout_seconds)),
                        "options": f"-c statement_timeout={max(1, int(settings.database_command_timeout_seconds * 1000))}",
                    },
                    open=True,
                )
            with self._pool.connection() as pooled:
                try:
                    yield pooled
                    pooled.commit()
                except Exception:
                    try:
                        pooled.rollback()
                    except Exception:
                        pass
                    raise
            return
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _ensure_source_payload_column(self) -> None:
        if self._source_payload_ready:
            return
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""ALTER TABLE IF EXISTS {self.schema}.processing_jobs
                        ADD COLUMN IF NOT EXISTS source_payload jsonb"""
                )
        self._source_payload_ready = True

    def create_or_get_job(self, envelope: dict[str, Any], *, processing_version: str | None = None) -> dict[str, Any]:
        self._ensure_source_payload_column()
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        source_event_id = str(envelope.get("event_id") or "")
        if not source_event_id:
            raise ValueError("event_id is required")
        knowledge_item_id = str(payload.get("knowledge_item_id") or "")
        resource_type = str(payload.get("resource_type") or "")
        resource_id = str(payload.get("resource_id") or "")
        if not knowledge_item_id or resource_type not in {"message", "attachment"} or not resource_id:
            raise ValueError("knowledge.ready payload is missing resource identity")
        hash_value = payload_hash(payload)
        audience = str(payload.get("source_audience_policy") or "")
        scope_type, scope_id = _resolve_job_scope(payload, envelope)
        knowledge_base_id = str(payload.get("knowledge_base_id") or ZERO_UUID)
        version = processing_version or settings.processing_version
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.processing_jobs
                    (source_event_id,payload_hash,job_type,knowledge_item_id,resource_type,resource_id,
                     knowledge_base_id,scope_type,scope_id,source_conversation_id,source_audience_policy,
                     content_version,processing_version,acl_version,source_payload)
                    VALUES (%s::uuid,%s,'full_process',%s::uuid,%s,%s::uuid,%s::uuid,%s,%s::uuid,
                            %s::uuid,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (source_event_id) DO UPDATE SET
                        source_payload=EXCLUDED.source_payload
                    RETURNING id::text,payload_hash,status,current_stage""",
                    (
                        source_event_id, hash_value, knowledge_item_id, resource_type, resource_id,
                        knowledge_base_id, scope_type, scope_id,
                        _uuid_or_none(payload.get("source_conversation_id")), audience or None,
                        int(payload.get("content_version") or 1), version,
                        int(payload.get("acl_version") or 0),
                        _as_json(payload),
                    ),
                )
                row = cursor.fetchone()
                if row and str(row[1]) != hash_value:
                    raise ValueError("source_event_id payload changed")
                job_id = str(row[0])
                cursor.execute(
                    f"""SELECT id::text,status,current_stage,lease_owner,lease_until,lease_epoch,retry_count,next_retry_at,parse_status
                        FROM {self.schema}.processing_jobs WHERE id=%s::uuid""",
                    (job_id,),
                )
                return self._job_row(cursor.fetchone())

    def get_job(self, job_id: str | None = None, *, source_event_id: str | None = None) -> dict[str, Any] | None:
        if not job_id and not source_event_id:
            return None
        where, value = ("id=%s::uuid", job_id) if job_id else ("source_event_id=%s::uuid", source_event_id)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,status,current_stage,lease_owner,lease_until,lease_epoch,retry_count,next_retry_at,parse_status,
                               source_event_id::text,knowledge_item_id::text,resource_type,resource_id::text,
                               knowledge_base_id::text,scope_type,scope_id::text,source_conversation_id::text,
                               source_audience_policy,content_version,processing_version,acl_version,last_error,
                               source_payload
                        FROM {self.schema}.processing_jobs WHERE {where}""",
                    (value,),
                )
                row = cursor.fetchone()
                return self._full_job_row(row) if row else None

    def _lane_stage_clause(self, lane: str) -> str:
        try:
            return {
                "parse": "((status='pending' OR status='retry_wait' OR status='processing') AND (current_stage IS NULL OR current_stage IN ('fetch','parse','chunk')))",
                "index": "(status IN ('processing','retry_wait') AND current_stage='index')",
                "memory": "(status='ready' AND current_stage='memory')",
            }[lane]
        except KeyError as exc:
            raise ValueError("lane must be parse, index, or memory") from exc

    def claim_jobs(
        self,
        lane: str,
        *,
        limit: int = 1,
        lease_seconds: int | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stage_clause = self._lane_stage_clause(lane)
        lease = int(lease_seconds or settings.task_lease_seconds)
        owner = f"{settings.service_name}:{lane}:{uuid.uuid4().hex[:12]}"
        job_clause = "AND id=%s::uuid" if job_id else ""
        job_params: tuple[Any, ...] = (job_id,) if job_id else ()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""WITH candidates AS (
                          SELECT id FROM {self.schema}.processing_jobs
                          WHERE {stage_clause}
                            {job_clause}
                            AND next_retry_at<=CURRENT_TIMESTAMP
                            AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
                          ORDER BY created_at
                          FOR UPDATE SKIP LOCKED
                          LIMIT %s
                        )
                        UPDATE {self.schema}.processing_jobs j
                        SET lease_owner=%s,lease_epoch=j.lease_epoch+1,
                            lease_until=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                            status='processing',
                            current_stage=COALESCE(j.current_stage,%s),
                            started_at=COALESCE(j.started_at,CURRENT_TIMESTAMP),
                            updated_at=CURRENT_TIMESTAMP
                        FROM candidates c WHERE j.id=c.id
                        RETURNING j.id::text,j.status,j.current_stage,j.lease_owner,j.lease_until,
                                  j.lease_epoch,j.retry_count,j.next_retry_at,j.parse_status,j.source_event_id::text,
                                  j.knowledge_item_id::text,j.resource_type,j.resource_id::text,
                                  j.knowledge_base_id::text,j.scope_type,j.scope_id::text,
                                  j.source_conversation_id::text,j.source_audience_policy,
                                  j.content_version,j.processing_version,j.acl_version,j.last_error,
                                  j.source_payload""",
                    (
                        *job_params,
                        limit,
                        owner,
                        lease,
                        "fetch" if lane == "parse" else lane,
                    ),
                )
                return [self._full_job_row(row) for row in cursor.fetchall()]

    def list_recoverable_jobs(
        self,
        lane: str,
        *,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        stage_clause = self._lane_stage_clause(lane)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,status,current_stage,lease_owner,lease_until,lease_epoch,
                               retry_count,next_retry_at,parse_status,source_event_id::text,
                               knowledge_item_id::text,resource_type,resource_id::text,
                               knowledge_base_id::text,scope_type,scope_id::text,
                               source_conversation_id::text,source_audience_policy,
                               content_version,processing_version,acl_version,last_error,
                               source_payload
                        FROM {self.schema}.processing_jobs
                        WHERE {stage_clause}
                          AND next_retry_at<=CURRENT_TIMESTAMP
                          AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
                        ORDER BY created_at
                        LIMIT %s""",
                    (max(1, limit),),
                )
                return [self._full_job_row(row) for row in cursor.fetchall()]

    def heartbeat(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        lease_seconds: int | None = None,
    ) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.processing_jobs
                        SET lease_until=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid AND lease_owner=%s AND lease_epoch=%s""",
                    (
                        int(lease_seconds or settings.task_lease_seconds),
                        job_id,
                        owner,
                        int(epoch),
                    ),
                )
                return cursor.rowcount == 1

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "knowledge_base_id", "scope_type", "scope_id", "source_conversation_id",
            "source_audience_policy", "status", "current_stage", "lease_owner", "lease_until",
            "lease_epoch", "retry_count", "next_retry_at", "parse_status", "last_error",
            "started_at", "finished_at",
        }
        values = [(key, value) for key, value in fields.items() if key in allowed]
        if not values:
            return
        assignments = []
        params: list[Any] = []
        for key, value in values:
            if key in {"knowledge_base_id", "scope_id", "source_conversation_id"}:
                assignments.append(f"{key}=%s::uuid")
            elif key in {"lease_until", "next_retry_at", "started_at", "finished_at"}:
                assignments.append(f"{key}=%s::timestamptz")
            else:
                assignments.append(f"{key}=%s")
            params.append(value)
        assignments.append("updated_at=CURRENT_TIMESTAMP")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                if "status" in fields:
                    cursor.execute(
                        f"""SELECT status FROM {self.schema}.processing_jobs
                            WHERE id=%s::uuid FOR UPDATE""",
                        (job_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        validate_job_transition(str(row[0]), str(fields["status"]))
                cursor.execute(
                    f"UPDATE {self.schema}.processing_jobs SET {', '.join(assignments)} WHERE id=%s::uuid",
                    (*params, job_id),
                )

    def update_job_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        return self._update_job_if_owned(
            job_id,
            owner=owner,
            epoch=epoch,
            fields=fields,
        )

    def complete_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        return self._update_job_if_owned(
            job_id,
            owner=owner,
            epoch=epoch,
            fields={**fields, "status": fields.get("status", "ready")},
        )

    def fail_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        return self._update_job_if_owned(
            job_id,
            owner=owner,
            epoch=epoch,
            fields={**fields, "status": "failed"},
        )

    def _update_job_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        allowed = {
            "knowledge_base_id", "scope_type", "scope_id", "source_conversation_id",
            "source_audience_policy", "status", "current_stage", "lease_owner",
            "lease_until", "retry_count", "next_retry_at", "parse_status",
            "last_error", "started_at", "finished_at",
        }
        values = [(key, value) for key, value in fields.items() if key in allowed]
        assignments = []
        params: list[Any] = []
        for key, value in values:
            if key in {"knowledge_base_id", "scope_id", "source_conversation_id"}:
                assignments.append(f"{key}=%s::uuid")
            elif key in {"lease_until", "next_retry_at", "started_at", "finished_at"}:
                assignments.append(f"{key}=%s::timestamptz")
            else:
                assignments.append(f"{key}=%s")
            params.append(value)
        assignments.append("updated_at=CURRENT_TIMESTAMP")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                if "status" in fields:
                    cursor.execute(
                        f"""SELECT status FROM {self.schema}.processing_jobs
                            WHERE id=%s::uuid AND lease_owner=%s AND lease_epoch=%s
                            FOR UPDATE""",
                        (job_id, owner, int(epoch)),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return False
                    validate_job_transition(str(row[0]), str(fields["status"]))
                cursor.execute(
                    f"""UPDATE {self.schema}.processing_jobs
                        SET {', '.join(assignments)}
                        WHERE id=%s::uuid AND lease_owner=%s AND lease_epoch=%s""",
                    (*params, job_id, owner, int(epoch)),
                )
                return cursor.rowcount == 1

    def add_attempt(
        self,
        job_id: str,
        *,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
        lane: str,
        stage: str,
        status: str,
        retryable: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                if lease_owner is not None and lease_epoch is not None:
                    cursor.execute(
                        f"""SELECT 1 FROM {self.schema}.processing_jobs
                            WHERE id=%s::uuid AND lease_owner=%s AND lease_epoch=%s""",
                        (job_id, lease_owner, int(lease_epoch)),
                    )
                    if not cursor.fetchone():
                        raise RuntimeError("lease lost")
                cursor.execute(
                    f"""SELECT COALESCE(MAX(attempt),0)+1 FROM {self.schema}.processing_job_attempts
                        WHERE job_id=%s::uuid AND stage=%s""",
                    (job_id, stage),
                )
                attempt = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""INSERT INTO {self.schema}.processing_job_attempts
                    (job_id,lane,stage,attempt,status,retryable,error_code,error_message,metrics,
                     lease_owner,lease_epoch,finished_at)
                    VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                            %s,%s,
                            CASE WHEN %s='running' THEN NULL ELSE CURRENT_TIMESTAMP END)
                    RETURNING id::text""",
                    (
                        job_id, lane, stage, attempt, status, retryable, error_code,
                        (error_message or "")[:2000] or None, _as_json(metrics),
                        lease_owner, lease_epoch, status,
                    ),
                )
                return str(cursor.fetchone()[0])

    def upsert_snapshot(self, context: ResourceContext) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.resource_snapshots
                    (knowledge_item_id,resource_type,resource_id,resource_ref,knowledge_base_id,
                     scope_type,scope_id,source_conversation_id,source_conversation_type,
                     source_audience_policy,external_conversation_id,object_ref,sender_identity_id,
                     sender_platform,sender_workspace_key,sender_external_user_id,sender_mapped_user_id,
                     sender_display_name,content_version,content_hash,acl_version,access_scope,sensitivity,
                     has_display_content,has_protected_content,lifecycle_status)
                    VALUES (%s::uuid,%s,%s::uuid,%s,%s::uuid,%s,%s::uuid,%s::uuid,%s,%s,%s,%s,
                            %s::uuid,%s,%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (knowledge_item_id,content_version) DO UPDATE SET
                      resource_ref=EXCLUDED.resource_ref,knowledge_base_id=EXCLUDED.knowledge_base_id,
                      scope_type=EXCLUDED.scope_type,scope_id=EXCLUDED.scope_id,
                      source_conversation_id=EXCLUDED.source_conversation_id,
                      source_conversation_type=EXCLUDED.source_conversation_type,
                      source_audience_policy=EXCLUDED.source_audience_policy,
                      external_conversation_id=EXCLUDED.external_conversation_id,
                      object_ref=EXCLUDED.object_ref,sender_identity_id=EXCLUDED.sender_identity_id,
                      sender_platform=EXCLUDED.sender_platform,
                      sender_workspace_key=EXCLUDED.sender_workspace_key,
                      sender_external_user_id=EXCLUDED.sender_external_user_id,
                      sender_mapped_user_id=EXCLUDED.sender_mapped_user_id,
                      sender_display_name=EXCLUDED.sender_display_name,
                      content_hash=EXCLUDED.content_hash,acl_version=EXCLUDED.acl_version,
                      access_scope=EXCLUDED.access_scope,sensitivity=EXCLUDED.sensitivity,
                      has_display_content=EXCLUDED.has_display_content,
                      has_protected_content=EXCLUDED.has_protected_content,
                      lifecycle_status=EXCLUDED.lifecycle_status,updated_at=CURRENT_TIMESTAMP
                    RETURNING id::text""",
                    (
                        context.knowledge_item_id, context.resource_type, context.resource_id,
                        context.resource_ref, context.knowledge_base_id, context.scope_type,
                        context.scope_id, context.source_conversation_id, context.source_conversation_type,
                        context.source_audience_policy, context.external_conversation_id,
                        context.object_ref, context.sender_identity_id, context.sender_platform,
                        context.sender_workspace_key, context.sender_external_user_id,
                        context.sender_mapped_user_id, context.sender_display_name,
                        context.content_version, context.content_hash, context.acl_version,
                        context.access_scope, context.sensitivity, context.has_display_content,
                        context.has_protected_content, context.lifecycle_status,
                    ),
                )
                return str(cursor.fetchone()[0])

    def upsert_chunks(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for chunk in chunks:
                    cursor.execute(
                        f"""INSERT INTO {self.schema}.chunks
                        (chunk_id,resource_snapshot_id,knowledge_item_id,resource_type,resource_id,
                         knowledge_base_id,scope_type,scope_id,source_conversation_id,conversation_type,
                         document_id,message_id,content_version,processing_version,chunking_version,
                         content_variant,chunk_index,chunk_count,title,file_name,heading_path,context_header,
                         content,content_hash,source_locator,sent_at,auth_partition_key,auth_object_key,
                         acl_version,sensitivity,embedding_model,embedding_dimensions,embedding_status,
                         rag_eligible,lifecycle_status)
                        VALUES (%s,%s::uuid,%s::uuid,%s,%s::uuid,%s::uuid,%s,%s::uuid,%s::uuid,%s,
                                %s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,
                                %s::timestamptz,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (chunk_id) DO UPDATE SET
                          resource_snapshot_id=EXCLUDED.resource_snapshot_id,
                          title=EXCLUDED.title,file_name=EXCLUDED.file_name,
                          heading_path=EXCLUDED.heading_path,context_header=EXCLUDED.context_header,
                          content=EXCLUDED.content,content_hash=EXCLUDED.content_hash,
                          source_locator=EXCLUDED.source_locator,sent_at=EXCLUDED.sent_at,
                          auth_partition_key=EXCLUDED.auth_partition_key,
                          auth_object_key=EXCLUDED.auth_object_key,acl_version=EXCLUDED.acl_version,
                          sensitivity=EXCLUDED.sensitivity,embedding_status='pending',
                          rag_eligible=EXCLUDED.rag_eligible,lifecycle_status=EXCLUDED.lifecycle_status,
                          updated_at=CURRENT_TIMESTAMP""",
                        (
                            chunk.chunk_id, chunk.resource_snapshot_id, chunk.knowledge_item_id,
                            chunk.resource_type, chunk.resource_id, chunk.knowledge_base_id,
                            chunk.scope_type, chunk.scope_id, chunk.source_conversation_id,
                            chunk.conversation_type, chunk.document_id, chunk.message_id,
                            chunk.content_version, chunk.processing_version, chunk.chunking_version,
                            chunk.content_variant, chunk.chunk_index, chunk.chunk_count, chunk.title,
                            chunk.file_name, list(chunk.heading_path), _as_json(chunk.context_header),
                            chunk.content, chunk.content_hash, _as_json(chunk.source_locator),
                            chunk.sent_at, chunk.auth_partition_key, chunk.auth_object_key,
                            chunk.acl_version, chunk.sensitivity, chunk.embedding_model,
                            chunk.embedding_dimensions, chunk.embedding_status, chunk.rag_eligible,
                            chunk.lifecycle_status,
                        ),
                    )
                return len(chunks)

    def list_chunks(
        self,
        *,
        snapshot_id: str | None = None,
        chunk_ids: list[str] | None = None,
        variant: str | None = None,
        embedding_status: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> list[Chunk]:
        filters = []
        params: list[Any] = []
        if snapshot_id:
            filters.append("resource_snapshot_id=%s::uuid")
            params.append(snapshot_id)
        if chunk_ids:
            filters.append("chunk_id=ANY(%s)")
            params.append(chunk_ids)
        if variant:
            filters.append("content_variant=%s")
            params.append(variant)
        if embedding_status:
            filters.append("embedding_status=%s")
            params.append(embedding_status)
        if scope_type:
            filters.append("scope_type=%s")
            params.append(scope_type)
        if scope_id:
            filters.append("scope_id=%s::uuid")
            params.append(scope_id)
        where = " AND ".join(filters) if filters else "TRUE"
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT {self._chunk_select()}
                        FROM {self.schema}.chunks WHERE {where} ORDER BY chunk_index""",
                    tuple(params),
                )
                return [self._chunk_row(row) for row in cursor.fetchall()]

    def _chunk_select(self) -> str:
        """Column list shared by chunk queries, including the entity mounts."""
        return f"""chunk_id,resource_snapshot_id::text,knowledge_item_id::text,resource_type,
                   resource_id::text,knowledge_base_id::text,scope_type,scope_id::text,
                   content_version,processing_version,chunking_version,content_variant,
                   chunk_index,chunk_count,title,file_name,heading_path,context_header,content,
                   content_hash,source_locator,source_conversation_id::text,conversation_type,
                   document_id::text,message_id::text,sent_at,auth_partition_key,auth_object_key,
                   acl_version,sensitivity,embedding_model,embedding_dimensions,
                   embedding_status,rag_eligible,lifecycle_status,
                   COALESCE((SELECT array_agg(b.entity_id::text ORDER BY b.entity_id::text)
                             FROM {self.schema}.chunk_branches b
                             WHERE b.chunk_id={self.schema}.chunks.chunk_id
                               AND b.status='active'),ARRAY[]::text[]),
                   COALESCE((SELECT jsonb_agg(jsonb_build_object(
                                       'entity_id', b.entity_id::text,
                                       'domain', e.domain,
                                       'confidence', b.confidence,
                                       'method', b.mount_method)
                                   ORDER BY b.entity_id::text)
                             FROM {self.schema}.chunk_branches b
                             JOIN {self.schema}.entity_registry e ON e.id=b.entity_id
                             WHERE b.chunk_id={self.schema}.chunks.chunk_id
                               AND b.status='active'),'[]'::jsonb),
                   COALESCE((SELECT MAX(registry_version)
                             FROM {self.schema}.chunk_branches b
                             WHERE b.chunk_id={self.schema}.chunks.chunk_id
                               AND b.status='active'),0)"""

    # ---------------------------------------------------------- window scan

    def list_scan_conversations(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Conversations holding messages newer than their scan watermark."""
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT c.scope_type,c.scope_id::text,c.source_conversation_id::text,
                               COUNT(*) AS pending,MIN(c.sent_at) AS oldest
                        FROM {self.schema}.chunks c
                        LEFT JOIN {self.schema}.entity_scan_watermarks w
                          ON w.scope_type=c.scope_type AND w.scope_id=c.scope_id
                         AND w.conversation_id=c.source_conversation_id
                        WHERE c.resource_type='message'
                          AND c.source_conversation_id IS NOT NULL
                          AND c.lifecycle_status='active'
                          AND c.sent_at IS NOT NULL
                          AND (w.last_sent_at IS NULL OR c.sent_at > w.last_sent_at)
                        GROUP BY 1,2,3
                        ORDER BY MIN(c.sent_at)
                        LIMIT %s""",
                    (max(1, int(limit)),),
                )
                return [
                    {
                        "scope_type": row[0], "scope_id": row[1],
                        "conversation_id": row[2], "pending": int(row[3]),
                        "last_sent_at": row[4].isoformat() if row[4] else None,
                    }
                    for row in cursor.fetchall()
                ]

    def get_scan_watermark(
        self, *, scope_type: str, scope_id: str, conversation_id: str
    ) -> str | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT last_sent_at FROM {self.schema}.entity_scan_watermarks
                        WHERE scope_type=%s AND scope_id=%s::uuid AND conversation_id=%s::uuid""",
                    (scope_type, scope_id, conversation_id),
                )
                row = cursor.fetchone()
                return row[0].isoformat() if row and row[0] else None

    def list_conversation_chunks(
        self,
        *,
        scope_type: str,
        scope_id: str,
        conversation_id: str,
        after_sent_at: str | None = None,
        limit: int = 500,
    ) -> list[Chunk]:
        """Messages of one conversation, oldest first, after the watermark."""
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT {self._chunk_select()}
                        FROM {self.schema}.chunks
                        WHERE scope_type=%s AND scope_id=%s::uuid
                          AND source_conversation_id=%s::uuid
                          AND resource_type='message'
                          AND lifecycle_status='active'
                          AND sent_at IS NOT NULL
                          AND (%s::timestamptz IS NULL OR sent_at > %s::timestamptz)
                        ORDER BY sent_at, chunk_id
                        LIMIT %s""",
                    (
                        scope_type, scope_id, conversation_id,
                        after_sent_at, after_sent_at, max(1, int(limit)),
                    ),
                )
                return [self._chunk_row(row) for row in cursor.fetchall()]

    def set_scan_watermark(
        self,
        *,
        scope_type: str,
        scope_id: str,
        conversation_id: str,
        last_sent_at: str,
        last_chunk_id: str | None = None,
        window_count: int = 0,
    ) -> None:
        """Advance the watermark. GREATEST keeps it from ever moving backwards,
        so a stale batch cannot cause the same messages to be rescanned."""
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_scan_watermarks
                        (scope_type,scope_id,conversation_id,last_sent_at,last_chunk_id,window_count)
                        VALUES (%s,%s::uuid,%s::uuid,%s::timestamptz,%s,%s)
                        ON CONFLICT (scope_type,scope_id,conversation_id) DO UPDATE SET
                          last_sent_at=GREATEST(
                              {self.schema}.entity_scan_watermarks.last_sent_at,
                              EXCLUDED.last_sent_at),
                          last_chunk_id=EXCLUDED.last_chunk_id,
                          window_count={self.schema}.entity_scan_watermarks.window_count
                                       + EXCLUDED.window_count,
                          updated_at=CURRENT_TIMESTAMP""",
                    (
                        scope_type, scope_id, conversation_id, last_sent_at,
                        last_chunk_id, max(0, int(window_count)),
                    ),
                )

    def find_entities_by_normalized(
        self, *, scope_type: str, scope_id: str, normalized_keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Map normalized name -> active entity, for turning LLM output into mounts."""
        keys = [key for key in dict.fromkeys(normalized_keys) if key]
        if not keys:
            return {}
        found: dict[str, dict[str, Any]] = {}
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT e.normalized_key,e.id::text,e.domain,e.canonical_name,e.registry_version
                        FROM {self.schema}.entity_registry e
                        WHERE e.scope_type=%s AND e.scope_id=%s::uuid AND e.status='active'
                          AND e.normalized_key=ANY(%s)""",
                    (scope_type, scope_id, keys),
                )
                for row in cursor.fetchall():
                    found[str(row[0])] = {
                        "entity_id": row[1], "domain": row[2], "canonical_name": row[3],
                        "registry_version": int(row[4]),
                    }
                cursor.execute(
                    f"""SELECT a.normalized_alias,e.id::text,e.domain,e.canonical_name,e.registry_version
                        FROM {self.schema}.entity_aliases a
                        JOIN {self.schema}.entity_registry e ON e.id=a.entity_id
                        WHERE a.scope_type=%s AND a.scope_id=%s::uuid AND a.status='active'
                          AND a.normalized_alias=ANY(%s)""",
                    (scope_type, scope_id, keys),
                )
                for row in cursor.fetchall():
                    found.setdefault(str(row[0]), {
                        "entity_id": row[1], "domain": row[2], "canonical_name": row[3],
                        "registry_version": int(row[4]),
                    })
        return found

    def upsert_entity_relation(
        self,
        *,
        scope_type: str,
        scope_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        confidence: float,
        evidence_chunk_ids: list[str] | None = None,
    ) -> None:
        """Write a relation, keeping the strongest confidence and all evidence.

        The same relation is re-observed across overlapping windows, so a plain
        upsert would let a later low-confidence sighting overwrite a high one and
        would drop the earlier supporting chunks.
        """
        evidence = [value for value in dict.fromkeys(evidence_chunk_ids or []) if value]
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_relations
                        (scope_type,scope_id,source_entity_id,target_entity_id,
                         relation_type,confidence,evidence_chunk_ids)
                        VALUES (%s,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s::char(64)[])
                        ON CONFLICT (scope_type,scope_id,source_entity_id,target_entity_id,relation_type)
                        DO UPDATE SET
                          confidence=GREATEST(
                              {self.schema}.entity_relations.confidence, EXCLUDED.confidence),
                          evidence_chunk_ids=ARRAY(
                              SELECT DISTINCT value
                              FROM unnest({self.schema}.entity_relations.evidence_chunk_ids
                                          || EXCLUDED.evidence_chunk_ids) AS value),
                          updated_at=CURRENT_TIMESTAMP""",
                    (
                        scope_type, scope_id, source_entity_id, target_entity_id,
                        relation_type, float(confidence), evidence,
                    ),
                )

    def tree_metrics(self, *, scope_type: str, scope_id: str) -> dict[str, Any]:
        """Counts that need no labelled data, so they can be watched from day one."""
        scope_params = (scope_type, scope_id)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT
                          (SELECT COUNT(*) FROM {self.schema}.chunks
                            WHERE scope_type=%s AND scope_id=%s::uuid
                              AND resource_type='message' AND lifecycle_status='active'),
                          (SELECT COUNT(DISTINCT chunk_id) FROM {self.schema}.chunk_branches
                            WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'),
                          (SELECT COUNT(*) FROM {self.schema}.chunk_branches
                            WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'),
                          (SELECT COUNT(*) FROM {self.schema}.entity_registry
                            WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'),
                          (SELECT COUNT(*) FROM {self.schema}.entity_registry
                            WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'
                              AND embedding_status<>'ready'),
                          (SELECT COUNT(*) FROM {self.schema}.entity_candidates
                            WHERE scope_type=%s AND scope_id=%s::uuid AND status IN ('new','review_ready')),
                          (SELECT COUNT(*) FROM {self.schema}.entity_relations
                            WHERE scope_type=%s AND scope_id=%s::uuid),
                          (SELECT COUNT(*) FROM {self.schema}.entity_scan_watermarks
                            WHERE scope_type=%s AND scope_id=%s::uuid)""",
                    scope_params * 8,
                )
                row = cursor.fetchone()
                message_count = int(row[0] or 0)
                mounted_chunks = int(row[1] or 0)
                metrics: dict[str, Any] = {
                    "message_count": message_count,
                    "mounted_chunk_count": mounted_chunks,
                    "mount_count": int(row[2] or 0),
                    "entity_count": int(row[3] or 0),
                    "entities_missing_embedding": int(row[4] or 0),
                    "pending_candidate_count": int(row[5] or 0),
                    "relation_count": int(row[6] or 0),
                    "scanned_conversation_count": int(row[7] or 0),
                    # Coverage is the cold-start signal: an empty tree scores 0
                    # and every downstream quality number is meaningless.
                    "mount_coverage": round(mounted_chunks / message_count, 4) if message_count else 0.0,
                }
                cursor.execute(
                    f"""SELECT mount_method,COUNT(*),MIN(confidence),AVG(confidence)
                        FROM {self.schema}.chunk_branches
                        WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'
                        GROUP BY mount_method ORDER BY mount_method""",
                    scope_params,
                )
                metrics["mount_methods"] = {
                    str(item[0]): {
                        "count": int(item[1]),
                        "min_confidence": float(item[2] or 0),
                        "avg_confidence": round(float(item[3] or 0), 4),
                    }
                    for item in cursor.fetchall()
                }
                cursor.execute(
                    f"""SELECT domain,COUNT(*) FROM {self.schema}.entity_registry
                        WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'
                        GROUP BY domain ORDER BY domain""",
                    scope_params,
                )
                metrics["entities_by_domain"] = {
                    str(item[0]): int(item[1]) for item in cursor.fetchall()
                }
                cursor.execute(
                    f"""SELECT relation_type,COUNT(*) FROM {self.schema}.entity_relations
                        WHERE scope_type=%s AND scope_id=%s::uuid
                        GROUP BY relation_type ORDER BY relation_type""",
                    scope_params,
                )
                metrics["relations_by_type"] = {
                    str(item[0]): int(item[1]) for item in cursor.fetchall()
                }
                return metrics

    def find_related_entities(
        self,
        *,
        scope_type: str,
        scope_id: str,
        entity_ids: list[str],
        relation_types: list[str] | None = None,
        direction: str = "both",
        min_confidence: float = 0.7,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """One-hop neighbours of the given entities.

        Depth is fixed at one: the design's relation depth is 1-2 hops and every
        extra hop multiplies the candidate set, so traversal stays bounded here
        and the caller decides whether to widen.
        """
        seeds = [value for value in dict.fromkeys(entity_ids) if value]
        if not seeds:
            return []
        # Parameter order follows the SQL text, and the ON clause is rendered
        # before the WHERE clause, so the neighbour placeholder comes first.
        params: list[Any] = []
        if direction == "outbound":
            neighbor_clause = "r.target_entity_id"
            match = "r.source_entity_id=ANY(%s::uuid[])"
            params.append(seeds)
        elif direction == "inbound":
            neighbor_clause = "r.source_entity_id"
            match = "r.target_entity_id=ANY(%s::uuid[])"
            params.append(seeds)
        else:
            neighbor_clause = (
                "CASE WHEN r.source_entity_id=ANY(%s::uuid[]) "
                "THEN r.target_entity_id ELSE r.source_entity_id END"
            )
            match = ("(r.source_entity_id=ANY(%s::uuid[]) "
                     "OR r.target_entity_id=ANY(%s::uuid[]))")
            params.extend([seeds, seeds, seeds])
        params.extend([scope_type, scope_id, float(min_confidence)])
        type_clause = ""
        if relation_types:
            type_clause = " AND r.relation_type=ANY(%s::text[])"
            params.append(list(relation_types))
        params.append(max(1, int(limit)))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT e.id::text,e.domain,e.canonical_name,
                               r.relation_type,r.confidence,
                               r.source_entity_id::text,r.target_entity_id::text
                        FROM {self.schema}.entity_relations r
                        JOIN {self.schema}.entity_registry e
                          ON e.id={neighbor_clause}
                        WHERE {match}
                          AND r.scope_type=%s AND r.scope_id=%s::uuid
                          AND r.confidence>=%s
                          AND e.status='active'
                          {type_clause}
                        ORDER BY r.confidence DESC, e.canonical_name
                        LIMIT %s""",
                    tuple(params),
                )
                return [
                    {
                        "entity_id": row[0], "domain": row[1], "canonical_name": row[2],
                        "relation_type": row[3], "confidence": float(row[4] or 0),
                        "source_entity_id": row[5], "target_entity_id": row[6],
                    }
                    for row in cursor.fetchall()
                ]

    def list_pending_branch_refresh_jobs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,scope_type,scope_id::text,entity_id::text,registry_version,
                               status,target_count,processed_count
                        FROM {self.schema}.branch_refresh_jobs
                        WHERE status IN ('pending','failed') AND next_retry_at<=CURRENT_TIMESTAMP
                        ORDER BY created_at LIMIT %s""",
                    (max(1, limit),),
                )
                return [
                    {
                        "id": row[0], "scope_type": row[1], "scope_id": row[2],
                        "entity_id": row[3], "registry_version": int(row[4]),
                        "status": row[5], "target_count": int(row[6] or 0),
                        "processed_count": int(row[7] or 0),
                    }
                    for row in cursor.fetchall()
                ]

    def get_branch_refresh_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,scope_type,scope_id::text,entity_id::text,registry_version,
                               status,target_count,processed_count,retry_count,last_error
                        FROM {self.schema}.branch_refresh_jobs WHERE id=%s::uuid""",
                    (job_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0], "scope_type": row[1], "scope_id": row[2],
                    "entity_id": row[3], "registry_version": int(row[4]),
                    "status": row[5], "target_count": int(row[6] or 0),
                    "processed_count": int(row[7] or 0), "retry_count": int(row[8] or 0),
                    "last_error": row[9],
                }

    def mark_branch_refresh(
        self,
        job_id: str,
        *,
        status: str,
        processed_count: int,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.branch_refresh_jobs
                        SET status=%s,processed_count=%s,last_error=%s,
                            started_at=CASE WHEN %s='processing' THEN COALESCE(started_at,CURRENT_TIMESTAMP) ELSE started_at END,
                            finished_at=CASE WHEN %s='succeeded' THEN CURRENT_TIMESTAMP ELSE finished_at END,
                            retry_count=retry_count+CASE WHEN %s='failed' THEN 1 ELSE 0 END,
                            next_retry_at=CASE WHEN %s='failed' THEN CURRENT_TIMESTAMP + INTERVAL '30 seconds' ELSE next_retry_at END,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid""",
                    (
                        status, processed_count, error, status, status, status, status, job_id,
                    ),
                )

    def update_embedding_status(
        self,
        chunk_ids: list[str],
        *,
        status: str,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        if not chunk_ids:
            return
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.chunks
                        SET embedding_status=%s,embedding_model=COALESCE(%s,embedding_model),
                            embedding_dimensions=COALESCE(%s,embedding_dimensions),updated_at=CURRENT_TIMESTAMP
                        WHERE chunk_id=ANY(%s)""",
                    (status, model, dimensions, chunk_ids),
                )

    def mark_older_chunks_inactive(self, *, knowledge_item_id: str, content_version: int) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.chunks SET lifecycle_status='inactive',updated_at=CURRENT_TIMESTAMP
                        WHERE knowledge_item_id=%s::uuid AND content_version<%s AND lifecycle_status='active'""",
                    (knowledge_item_id, content_version),
                )

    def delete_resource_data(self, *, knowledge_item_id: str, resource_id: str) -> int:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""DELETE FROM {self.schema}.chunks WHERE knowledge_item_id=%s::uuid OR resource_id=%s::uuid""",
                    (knowledge_item_id, resource_id),
                )
                return int(cursor.rowcount or 0)

    def upsert_projection(
        self,
        chunk: Chunk,
        *,
        alias: str,
        mapping_version: str,
        status: str,
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.projection_records
                    (chunk_id,chunk_variant,scope_type,scope_id,knowledge_base_id,es_index_alias,
                     es_document_id,mapping_version,embedding_model,status,indexed_at)
                    VALUES (%s,%s,%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,
                            CASE WHEN %s='ready' THEN CURRENT_TIMESTAMP ELSE NULL END)
                    ON CONFLICT (chunk_id,es_index_alias,mapping_version) DO UPDATE SET
                      es_index_alias=EXCLUDED.es_index_alias,embedding_model=EXCLUDED.embedding_model,
                      status=EXCLUDED.status,indexed_at=EXCLUDED.indexed_at,last_error=NULL,
                      updated_at=CURRENT_TIMESTAMP""",
                    (
                        chunk.chunk_id, chunk.content_variant, chunk.scope_type, chunk.scope_id,
                        chunk.knowledge_base_id, alias, chunk.chunk_id, mapping_version,
                        chunk.embedding_model, status, status,
                    ),
                )

    def ensure_projection_records(
        self,
        chunks: list[Chunk],
        *,
        mapping_version: str,
    ) -> dict[str, dict[str, Any]]:
        if not chunks:
            return {}
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for chunk in chunks:
                    alias = (
                        settings.elasticsearch_protected_write_index
                        if chunk.protected
                        else settings.elasticsearch_display_write_index
                    )
                    cursor.execute(
                        f"""INSERT INTO {self.schema}.projection_records
                        (chunk_id,chunk_variant,scope_type,scope_id,knowledge_base_id,
                         es_index_alias,es_document_id,mapping_version,embedding_model,
                         status,retry_count,next_retry_at)
                        VALUES (%s,%s,%s,%s::uuid,%s::uuid,%s,%s,%s,%s,'pending',0,CURRENT_TIMESTAMP)
                        ON CONFLICT (chunk_id,es_index_alias,mapping_version) DO NOTHING""",
                        (
                            chunk.chunk_id,
                            chunk.content_variant,
                            chunk.scope_type,
                            chunk.scope_id,
                            chunk.knowledge_base_id,
                            alias,
                            chunk.chunk_id,
                            mapping_version,
                            chunk.embedding_model,
                        ),
                    )
        return {
            item["chunk_id"]: item
            for item in self.list_projection_records(
                knowledge_item_id=chunks[0].knowledge_item_id,
                content_version=chunks[0].content_version,
            )
        }

    def list_projection_records(
        self,
        *,
        knowledge_item_id: str,
        content_version: int,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT p.chunk_id,p.chunk_variant,p.es_index_alias,p.mapping_version,
                               p.status,p.retry_count,p.next_retry_at,p.last_error,p.failure_stage
                        FROM {self.schema}.projection_records p
                        JOIN {self.schema}.chunks c ON c.chunk_id=p.chunk_id
                        WHERE c.knowledge_item_id=%s::uuid
                          AND c.content_version=%s
                          AND c.lifecycle_status='active'
                        ORDER BY p.chunk_id""",
                    (knowledge_item_id, int(content_version)),
                )
                return [
                    {
                        "chunk_id": row[0],
                        "chunk_variant": row[1],
                        "es_index_alias": row[2],
                        "mapping_version": row[3],
                        "status": row[4],
                        "retry_count": int(row[5] or 0),
                        "next_retry_at": row[6],
                        "last_error": row[7],
                        "failure_stage": row[8],
                    }
                    for row in cursor.fetchall()
                ]

    def update_projection_status(
        self,
        chunk_ids: list[str],
        *,
        status: str,
        failure_stage: str | None = None,
        error: str | None = None,
        increment_retry: bool = False,
        next_retry_at: datetime | None = None,
    ) -> int:
        if not chunk_ids:
            return 0
        ProjectionStatus(status)
        updated = 0
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for chunk_id in chunk_ids:
                    cursor.execute(
                        f"""SELECT status FROM {self.schema}.projection_records
                            WHERE chunk_id=%s FOR UPDATE""",
                        (chunk_id,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        continue
                    for row in rows:
                        validate_projection_transition(str(row[0]), status)
                    cursor.execute(
                        f"""UPDATE {self.schema}.projection_records
                            SET status=%s,
                                failure_stage=%s,
                                last_error=%s,
                                retry_count=retry_count + CASE WHEN %s THEN 1 ELSE 0 END,
                                next_retry_at=%s,
                                indexed_at=CASE WHEN %s='ready' THEN CURRENT_TIMESTAMP
                                                ELSE indexed_at END,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE chunk_id=%s""",
                        (
                            status,
                            failure_stage,
                            (error or "")[:2000] or None,
                            increment_retry,
                            next_retry_at,
                            status,
                            chunk_id,
                        ),
                    )
                    updated += cursor.rowcount
        return updated

    def load_entity_registry(self, *, scope_type: str, scope_id: str) -> tuple[list[Entity], list[EntityAlias], int]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,scope_type,scope_id::text,domain,canonical_name,normalized_key,
                               status,registry_version
                        FROM {self.schema}.entity_registry
                        WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'""",
                    (scope_type, scope_id),
                )
                entities = [Entity(*row[:8]) for row in cursor.fetchall()]
                cursor.execute(
                    f"""SELECT id::text,entity_id::text,scope_type,scope_id::text,domain,display_alias,
                               normalized_alias,status
                        FROM {self.schema}.entity_aliases
                        WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'""",
                    (scope_type, scope_id),
                )
                aliases = [EntityAlias(*row[:8]) for row in cursor.fetchall()]
                cursor.execute(
                    f"""SELECT COALESCE(MAX(registry_version),1)
                        FROM {self.schema}.entity_registry WHERE scope_type=%s AND scope_id=%s::uuid""",
                    (scope_type, scope_id),
                )
                version = int(cursor.fetchone()[0] or 1)
        return entities, aliases, version

    def locate_entities_exact(
        self, *, scope_type: str, scope_id: str, normalized: str
    ) -> list[dict[str, Any]]:
        """L1: canonical or alias equality within the scope.

        Returns every match rather than a single row: the same name may exist in
        two domains, and the caller decides which one the mention means.
        """
        if not normalized:
            return []
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT e.id::text,e.domain,e.canonical_name,e.registry_version,'exact'
                        FROM {self.schema}.entity_registry e
                        WHERE e.scope_type=%s AND e.scope_id=%s::uuid AND e.status='active'
                          AND e.normalized_key=%s
                        UNION
                        SELECT e.id::text,e.domain,e.canonical_name,e.registry_version,'alias'
                        FROM {self.schema}.entity_aliases a
                        JOIN {self.schema}.entity_registry e ON e.id=a.entity_id
                        WHERE a.scope_type=%s AND a.scope_id=%s::uuid AND a.status='active'
                          AND a.normalized_alias=%s""",
                    (scope_type, scope_id, normalized, scope_type, scope_id, normalized),
                )
                return [
                    {
                        "entity_id": row[0], "domain": row[1], "canonical_name": row[2],
                        "registry_version": int(row[3]), "match_method": row[4],
                        "match_score": 0.95 if row[4] == "exact" else 0.93,
                    }
                    for row in cursor.fetchall()
                ]

    def locate_entities_fuzzy(
        self, *, scope_type: str, scope_id: str, normalized: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """L2: trigram similarity over canonical names, normalized keys and aliases.

        Trigram similarity tolerates typos; it does not resolve abbreviations
        such as "aims" for "AIMS系统开发项目", which is L3's job. A plain
        containment match is included at a lower score because it still needs
        verification downstream.
        """
        if not normalized:
            return []
        limit = max(1, min(int(limit), 50))
        found: dict[str, dict[str, Any]] = {}

        def keep(
            entity_id: str, domain: str, name: str, version: int,
            score: float, method: str = "fuzzy",
        ) -> None:
            current = found.get(entity_id)
            if current is None or score > current["match_score"]:
                found[entity_id] = {
                    "entity_id": entity_id, "domain": domain, "canonical_name": name,
                    "registry_version": version, "match_method": method,
                    "match_score": round(float(score), 4),
                }

        def fuzzy_score(raw: float) -> float:
            """Trigram scores below 0.8 are not trustworthy enough to call a
            fuzzy match; map the accepted band onto 0.80-0.85."""
            return min(0.85, 0.80 + (float(raw) - 0.80) * 0.25)

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT e.id::text,e.domain,e.canonical_name,e.registry_version,
                               GREATEST(similarity(e.canonical_name,%s),
                                        similarity(e.normalized_key,%s))
                        FROM {self.schema}.entity_registry e
                        WHERE e.scope_type=%s AND e.scope_id=%s::uuid AND e.status='active'
                          AND (e.canonical_name %% %s OR e.normalized_key %% %s)
                        ORDER BY 5 DESC LIMIT %s""",
                    (normalized, normalized, scope_type, scope_id, normalized, normalized, limit),
                )
                for row in cursor.fetchall():
                    raw = float(row[4] or 0)
                    if raw < 0.8:
                        continue
                    keep(row[0], row[1], row[2], int(row[3]), fuzzy_score(raw))
                cursor.execute(
                    f"""SELECT e.id::text,e.domain,e.canonical_name,e.registry_version,
                               similarity(a.normalized_alias,%s)
                        FROM {self.schema}.entity_aliases a
                        JOIN {self.schema}.entity_registry e ON e.id=a.entity_id
                        WHERE a.scope_type=%s AND a.scope_id=%s::uuid AND a.status='active'
                          AND a.normalized_alias %% %s
                        ORDER BY 5 DESC LIMIT %s""",
                    (normalized, scope_type, scope_id, normalized, limit),
                )
                for row in cursor.fetchall():
                    raw = float(row[4] or 0)
                    if raw < 0.8:
                        continue
                    keep(row[0], row[1], row[2], int(row[3]), fuzzy_score(raw))
                # Containment catches omitted qualifiers ("青云" -> "青云飞鹏项目")
                # that a trigram score rates as too different to trust alone.
                if len(normalized) >= 2:
                    cursor.execute(
                        f"""SELECT e.id::text,e.domain,e.canonical_name,e.registry_version
                            FROM {self.schema}.entity_registry e
                            WHERE e.scope_type=%s AND e.scope_id=%s::uuid AND e.status='active'
                              AND e.normalized_key LIKE %s
                            ORDER BY length(e.normalized_key) LIMIT %s""",
                        (scope_type, scope_id, f"%{normalized}%", limit),
                    )
                    for row in cursor.fetchall():
                        keep(row[0], row[1], row[2], int(row[3]), 0.70, method="substring")
        ranked = sorted(found.values(), key=lambda item: (-item["match_score"], item["entity_id"]))
        return ranked[:limit]

    def locate_entities_semantic(
        self,
        *,
        scope_type: str,
        scope_id: str,
        embedding: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """L3: pgvector ANN over the scope, ordered by cosine distance.

        over_fetch widens the candidate list because a filtered ANN can return
        fewer than ``limit`` rows once the scope predicate is applied.
        """
        if not embedding:
            return []
        limit = max(1, min(int(limit), 50))
        over_fetch = min(limit * 5, 200)
        literal = _vector_literal(embedding)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT e.id::text,e.domain,e.canonical_name,e.registry_version,
                               1-(e.embedding <=> %s::vector) AS score
                        FROM {self.schema}.entity_registry e
                        WHERE e.scope_type=%s AND e.scope_id=%s::uuid AND e.status='active'
                          AND e.embedding IS NOT NULL
                        ORDER BY e.embedding <=> %s::vector
                        LIMIT %s""",
                    (literal, scope_type, scope_id, literal, over_fetch),
                )
                return [
                    {
                        "entity_id": row[0], "domain": row[1], "canonical_name": row[2],
                        "registry_version": int(row[3]), "match_method": "semantic",
                        "match_score": round(float(row[4] or 0), 4),
                    }
                    for row in cursor.fetchall()
                ][:limit]

    def list_entities_pending_embedding(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT e.id::text,e.scope_type,e.scope_id::text,e.domain,
                               e.canonical_name,COALESCE(e.description,''),
                               COALESCE(array_to_string(e.keywords,' '),''),
                               COALESCE(array_to_string(
                                   (SELECT array_agg(a.display_alias ORDER BY a.display_alias)
                                      FROM {self.schema}.entity_aliases a
                                     WHERE a.entity_id=e.id AND a.status='active'),' '),'')
                        FROM {self.schema}.entity_registry e
                        WHERE e.status='active' AND e.embedding_status IN ('pending','failed')
                        ORDER BY e.updated_at LIMIT %s""",
                    (max(1, int(limit)),),
                )
                return [
                    {
                        "entity_id": row[0], "scope_type": row[1], "scope_id": row[2],
                        "domain": row[3], "canonical_name": row[4], "description": row[5],
                        "keywords": row[6], "aliases": row[7],
                    }
                    for row in cursor.fetchall()
                ]

    def update_entity_embedding(
        self,
        *,
        entity_id: str,
        embedding: list[float],
        model: str,
        dimensions: int,
        status: str = "ready",
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.entity_registry
                        SET embedding=%s::vector,embedding_model=%s,embedding_dimensions=%s,
                            embedding_status=%s,embedding_updated_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid""",
                    (
                        _vector_literal(embedding) if embedding else None,
                        model, int(dimensions), status, entity_id,
                    ),
                )

    def upsert_entity(
        self,
        *,
        entity_id: str | None = None,
        scope_type: str,
        scope_id: str,
        domain: str,
        canonical_name: str,
        normalized_key: str,
        created_by: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT COALESCE(MAX(registry_version),0)+1
                        FROM {self.schema}.entity_registry
                        WHERE scope_type=%s AND scope_id=%s::uuid""",
                    (scope_type, scope_id),
                )
                registry_version = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_registry
                    (id,scope_type,scope_id,domain,canonical_name,normalized_key,status,registry_version,created_by)
                    VALUES (COALESCE(%s::uuid,gen_random_uuid()),%s,%s::uuid,%s,%s,%s,%s,%s,%s::uuid)
                    ON CONFLICT (scope_type,scope_id,domain,normalized_key) DO UPDATE SET
                      canonical_name=EXCLUDED.canonical_name,status=EXCLUDED.status,
                      registry_version=EXCLUDED.registry_version,updated_at=CURRENT_TIMESTAMP
                    RETURNING id::text,registry_version""",
                    (
                        entity_id, scope_type, scope_id, domain, canonical_name,
                        normalized_key, status, registry_version, created_by,
                    ),
                )
                row = cursor.fetchone()
                return {"id": row[0], "registry_version": int(row[1])}

    def upsert_alias(
        self,
        *,
        entity_id: str,
        scope_type: str,
        scope_id: str,
        domain: str,
        display_alias: str,
        normalized_alias: str,
        source: str = "manual",
    ) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_aliases
                    (entity_id,scope_type,scope_id,domain,display_alias,normalized_alias,source,status)
                    VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s,%s,'active')
                    ON CONFLICT (scope_type,scope_id,domain,normalized_alias) DO UPDATE SET
                      entity_id=EXCLUDED.entity_id,display_alias=EXCLUDED.display_alias,
                      source=EXCLUDED.source,status='active',updated_at=CURRENT_TIMESTAMP
                    RETURNING id::text""",
                    (
                        entity_id, scope_type, scope_id, domain, display_alias,
                        normalized_alias, source,
                    ),
                )
                return str(cursor.fetchone()[0])

    def list_entities(self, *, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,domain,canonical_name,normalized_key,status,
                               registry_version,merged_into_entity_id::text,created_at,updated_at
                        FROM {self.schema}.entity_registry
                        WHERE scope_type=%s AND scope_id=%s::uuid
                        ORDER BY domain,canonical_name""",
                    (scope_type, scope_id),
                )
                return [
                    {
                        "entity_id": row[0], "domain": row[1], "canonical_name": row[2],
                        "normalized_key": row[3], "status": row[4],
                        "registry_version": int(row[5]), "merged_into_entity_id": row[6],
                        "created_at": row[7].isoformat() if row[7] else None,
                        "updated_at": row[8].isoformat() if row[8] else None,
                    }
                    for row in cursor.fetchall()
                ]

    def get_entity(self, *, scope_type: str, scope_id: str, entity_id: str) -> dict[str, Any] | None:
        entities = [
            item for item in self.list_entities(scope_type=scope_type, scope_id=scope_id)
            if item["entity_id"] == entity_id
        ]
        if not entities:
            return None
        value = entities[0]
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,display_alias,normalized_alias,source,status
                        FROM {self.schema}.entity_aliases
                        WHERE entity_id=%s::uuid ORDER BY display_alias""",
                    (entity_id,),
                )
                value["aliases"] = [
                    {
                        "id": row[0], "display_alias": row[1], "normalized_alias": row[2],
                        "source": row[3], "status": row[4],
                    }
                    for row in cursor.fetchall()
                ]
        return value

    def replace_chunk_mounts(self, chunk: Chunk, mounts: list[EntityMount]) -> None:
        """Make the chunk's active mounts match ``mounts`` exactly.

        Used by the explicit index path, where a chunk's own text decides its
        entity set. The window scan uses :meth:`merge_chunk_mounts` instead:
        overlapping windows must add to each other rather than overwrite.
        """
        entity_ids = [mount.entity_id for mount in mounts]
        with self._connection() as connection:
            with connection.cursor() as cursor:
                if entity_ids:
                    cursor.execute(
                        f"""UPDATE {self.schema}.chunk_branches
                            SET status='removed',updated_at=CURRENT_TIMESTAMP
                            WHERE chunk_id=%s AND status='active'
                              AND NOT (entity_id=ANY(%s::uuid[]))""",
                        (chunk.chunk_id, entity_ids),
                    )
                else:
                    cursor.execute(
                        f"""UPDATE {self.schema}.chunk_branches
                            SET status='removed',updated_at=CURRENT_TIMESTAMP
                            WHERE chunk_id=%s AND status='active'""",
                        (chunk.chunk_id,),
                    )
                self._upsert_mount_rows(cursor, chunk, mounts)

    def merge_chunk_mounts(self, chunk: Chunk, mounts: list[EntityMount]) -> None:
        """Add mounts without removing any.

        A window only sees part of a conversation, so replacing here would let a
        later window erase what an earlier one contributed.
        """
        if not mounts:
            return
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._upsert_mount_rows(cursor, chunk, mounts)

    def _upsert_mount_rows(
        self, cursor: Any, chunk: Chunk, mounts: list[EntityMount]
    ) -> None:
        """Upsert on (chunk_id, entity_id) keeping the stronger evidence.

        Confidence only moves up and a stronger channel never gives way to a
        weaker one, which is what makes the 50%-overlap window scan idempotent.
        """
        for mount in mounts:
            cursor.execute(
                f"""INSERT INTO {self.schema}.chunk_branches
                (chunk_id,entity_id,scope_type,scope_id,registry_version,
                 confidence,mount_method,status)
                VALUES (%s,%s::uuid,%s,%s::uuid,%s,%s,%s,'active')
                ON CONFLICT (chunk_id,entity_id) DO UPDATE SET
                  registry_version=EXCLUDED.registry_version,
                  confidence=GREATEST({self.schema}.chunk_branches.confidence,
                                      EXCLUDED.confidence),
                  mount_method=CASE
                    WHEN EXCLUDED.mount_method='explicit'
                      OR {self.schema}.chunk_branches.mount_method='explicit'
                      THEN 'explicit'
                    WHEN EXCLUDED.mount_method='window_batch'
                      OR {self.schema}.chunk_branches.mount_method='window_batch'
                      THEN 'window_batch'
                    ELSE 'llm_infer'
                  END,
                  status='active',updated_at=CURRENT_TIMESTAMP""",
                (
                    chunk.chunk_id, mount.entity_id, chunk.scope_type,
                    chunk.scope_id, mount.registry_version, mount.confidence,
                    mount.mount_method,
                ),
            )

    def upsert_candidate_mention(
        self,
        *,
        scope_type: str,
        scope_id: str,
        candidate_name: str,
        normalized_key: str,
        domain: str,
        chunk: Chunk,
        context_excerpt: str,
        confidence: float = 0.5,
        method: str = "regex",
    ) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_candidates
                    (scope_type,scope_id,candidate_name,normalized_key,candidate_domain,mention_count,
                     distinct_chunk_count,distinct_source_count,distinct_conversation_count,
                     sample_context,score,status)
                    VALUES (%s,%s::uuid,%s,%s,%s,1,1,1,CASE WHEN %s::text='' THEN 0 ELSE 1 END,%s,%s,'new')
                    ON CONFLICT (scope_type,scope_id,candidate_domain,normalized_key) DO UPDATE SET
                      candidate_name=EXCLUDED.candidate_name,mention_count=entity_candidates.mention_count+1,
                      sample_context=COALESCE(entity_candidates.sample_context,EXCLUDED.sample_context),
                      score=GREATEST(entity_candidates.score,EXCLUDED.score),
                      last_seen_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    RETURNING id::text""",
                    (
                        scope_type, scope_id, candidate_name, normalized_key, domain,
                        chunk.source_conversation_id or "", context_excerpt[:500], confidence,
                    ),
                )
                candidate_id = str(cursor.fetchone()[0])
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_candidate_mentions
                    (candidate_id,chunk_id,scope_type,scope_id,surface_form,candidate_domain,
                     context_excerpt,confidence,extraction_method)
                    VALUES (%s::uuid,%s,%s,%s::uuid,%s,%s,%s,%s,%s)
                    ON CONFLICT (candidate_id,chunk_id,surface_form,extraction_method) DO NOTHING""",
                    (
                        candidate_id, chunk.chunk_id, scope_type, scope_id, candidate_name,
                        domain, context_excerpt[:500], confidence, method,
                    ),
                )
                return candidate_id

    def list_candidates(
        self,
        *,
        scope_type: str,
        scope_id: str,
        status: str | None = None,
        domain: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = ["scope_type=%s", "scope_id=%s::uuid"]
        params: list[Any] = [scope_type, scope_id]
        if status:
            conditions.append("status=%s")
            params.append(status)
        if domain:
            conditions.append("candidate_domain=%s")
            params.append(domain)
        if query:
            conditions.append("candidate_name ILIKE %s")
            params.append(f"%{query}%")
        where = " AND ".join(conditions)
        page, page_size = max(1, page), min(100, max(1, page_size))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {self.schema}.entity_candidates WHERE {where}", tuple(params))
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""SELECT id::text,candidate_name,candidate_domain,normalized_key,score,status,
                               mention_count,distinct_chunk_count,distinct_source_count,
                               distinct_conversation_count,suggested_entity_id::text,first_seen_at,
                               last_seen_at,resolved_entity_id::text
                        FROM {self.schema}.entity_candidates WHERE {where}
                        ORDER BY score DESC,last_seen_at DESC LIMIT %s OFFSET %s""",
                    (*params, page_size, (page - 1) * page_size),
                )
                return [self._candidate_row(row) for row in cursor.fetchall()], total

    def get_candidate(self, *, scope_type: str, scope_id: str, candidate_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,candidate_name,candidate_domain,normalized_key,score,status,
                               mention_count,distinct_chunk_count,distinct_source_count,
                               distinct_conversation_count,suggested_entity_id::text,first_seen_at,
                               last_seen_at,resolved_entity_id::text,review_note
                        FROM {self.schema}.entity_candidates
                        WHERE id=%s::uuid AND scope_type=%s AND scope_id=%s::uuid""",
                    (candidate_id, scope_type, scope_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                value = self._candidate_row(row)
                cursor.execute(
                    f"""SELECT id::text,chunk_id,surface_form,candidate_domain,context_excerpt,
                               confidence,extraction_method,created_at
                        FROM {self.schema}.entity_candidate_mentions
                        WHERE candidate_id=%s::uuid ORDER BY created_at DESC LIMIT 100""",
                    (candidate_id,),
                )
                value["mentions"] = [
                    {
                        "mention_id": item[0], "chunk_id": item[1], "surface_form": item[2],
                        "candidate_domain": item[3], "context_excerpt": item[4],
                        "confidence": float(item[5]), "extraction_method": item[6],
                        "created_at": item[7].isoformat() if item[7] else None,
                    }
                    for item in cursor.fetchall()
                ]
                return value

    def review_candidate(
        self,
        *,
        scope_type: str,
        scope_id: str,
        candidate_id: str,
        reviewer_id: str,
        review_request_id: str,
        action: str,
        expected_status: str | None,
        canonical_name: str | None = None,
        domain: str | None = None,
        target_entity_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"promote", "merge", "ignore", "defer"}:
            raise ValueError("unsupported review action")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,status,candidate_name,candidate_domain,resolved_entity_id::text,
                               normalized_key
                        FROM {self.schema}.entity_candidates
                        WHERE id=%s::uuid AND scope_type=%s AND scope_id=%s::uuid FOR UPDATE""",
                    (candidate_id, scope_type, scope_id),
                )
                row = cursor.fetchone()
                if not row:
                    raise LookupError("candidate not found")
                cursor.execute(
                    f"""SELECT result_status,registry_version FROM {self.schema}.entity_review_requests
                        WHERE candidate_id=%s::uuid AND review_request_id=%s::uuid""",
                    (candidate_id, review_request_id),
                )
                existing = cursor.fetchone()
                if existing:
                    return {
                        "candidate_id": candidate_id, "status": existing[0],
                        "resolved_entity_id": row[4], "registry_version": int(existing[1] or 1),
                        "idempotent": True,
                    }
                current_status = str(row[1])
                if expected_status and current_status != expected_status:
                    raise ValueError("candidate status changed")
                resolved_entity_id = row[4]
                candidate_normalized_key = row[5]
                registry_version: int | None = None
                if action == "promote":
                    name = canonical_name or row[2]
                    entity_domain = domain or row[3]
                    normalized = candidate_normalized_key
                    cursor.execute(
                        f"""INSERT INTO {self.schema}.entity_registry
                        (scope_type,scope_id,domain,canonical_name,normalized_key,status,registry_version,created_by)
                        VALUES (%s,%s::uuid,%s,%s,%s,'active',
                                COALESCE((SELECT MAX(registry_version)+1 FROM {self.schema}.entity_registry
                                          WHERE scope_type=%s AND scope_id=%s::uuid),1),%s::uuid)
                        RETURNING id::text,registry_version""",
                        (
                            scope_type, scope_id, entity_domain, name, normalized,
                            scope_type, scope_id, reviewer_id,
                        ),
                    )
                    resolved_entity_id, registry_version = cursor.fetchone()
                    resolved_entity_id = str(resolved_entity_id)
                    registry_version = int(registry_version)
                elif action == "merge":
                    if not target_entity_id:
                        raise ValueError("merge requires target_entity_id")
                    resolved_entity_id = target_entity_id
                    cursor.execute(
                        f"""SELECT registry_version FROM {self.schema}.entity_registry
                            WHERE id=%s::uuid AND scope_type=%s AND scope_id=%s::uuid""",
                        (target_entity_id, scope_type, scope_id),
                    )
                    target = cursor.fetchone()
                    if not target:
                        raise LookupError("target entity not found")
                    registry_version = int(target[0])
                    cursor.execute(
                        f"""INSERT INTO {self.schema}.entity_aliases
                        (entity_id,scope_type,scope_id,domain,display_alias,normalized_alias,source,status)
                        VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s,'candidate_merge','active')
                        ON CONFLICT (scope_type,scope_id,domain,normalized_alias) DO UPDATE SET
                          entity_id=EXCLUDED.entity_id,display_alias=EXCLUDED.display_alias,
                          source=EXCLUDED.source,status='active',updated_at=CURRENT_TIMESTAMP""",
                        (
                            resolved_entity_id,
                            scope_type,
                            scope_id,
                            str(row[3]),
                            str(row[2]),
                            candidate_normalized_key,
                        ),
                    )
                    # A new alias changes the text the entity should be embedded
                    # from, so the vector is refreshed instead of going stale.
                    cursor.execute(
                        f"""UPDATE {self.schema}.entity_registry
                            SET embedding_status='pending',updated_at=CURRENT_TIMESTAMP
                            WHERE id=%s::uuid""",
                        (resolved_entity_id,),
                    )
                result_status = {"promote": "promoted", "merge": "merged", "ignore": "ignored", "defer": "deferred"}[action]
                cursor.execute(
                    f"""UPDATE {self.schema}.entity_candidates
                        SET status=%s,resolved_entity_id=%s::uuid,reviewed_by=%s::uuid,
                            reviewed_at=CURRENT_TIMESTAMP,review_note=%s,updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid""",
                    (result_status, resolved_entity_id, reviewer_id, note, candidate_id),
                )
                cursor.execute(
                    f"""INSERT INTO {self.schema}.entity_review_requests
                    (candidate_id,review_request_id,action,target_entity_id,reviewer_id,expected_status,
                     result_status,registry_version)
                    VALUES (%s::uuid,%s::uuid,%s,%s::uuid,%s::uuid,%s,%s,%s)""",
                    (
                        candidate_id, review_request_id, action, target_entity_id, reviewer_id,
                        expected_status, result_status, registry_version,
                    ),
                )
                job_id = None
                if resolved_entity_id and action in {"promote", "merge"}:
                    cursor.execute(
                        f"""INSERT INTO {self.schema}.branch_refresh_jobs
                        (scope_type,scope_id,entity_id,registry_version,status,target_count)
                        VALUES (%s,%s::uuid,%s::uuid,%s, 'pending',
                                (SELECT COUNT(*) FROM {self.schema}.chunk_branches WHERE entity_id=%s::uuid))
                        ON CONFLICT DO NOTHING RETURNING id::text""",
                        (scope_type, scope_id, resolved_entity_id, int(registry_version or 1), resolved_entity_id),
                    )
                    refresh = cursor.fetchone()
                    job_id = str(refresh[0]) if refresh else None
                return {
                    "candidate_id": candidate_id, "status": result_status,
                    "resolved_entity_id": resolved_entity_id,
                    "registry_version": registry_version,
                    "branch_refresh_job_id": job_id,
                }

    def get_tree(self, *, scope_type: str, scope_id: str) -> dict[str, Any]:
        """Derive the admin tree from the registry and the mount table.

        tree_nodes was dropped in the v2 schema: the domain layer is the fixed
        set of five types and the entity layer is entity_registry itself, so a
        materialised copy only added a way for the two to drift apart.
        """
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT COALESCE(MAX(registry_version),1) FROM {self.schema}.entity_registry
                        WHERE scope_type=%s AND scope_id=%s::uuid""",
                    (scope_type, scope_id),
                )
                version = int(cursor.fetchone()[0] or 1)
                cursor.execute(
                    f"""SELECT domain,COUNT(*) FROM {self.schema}.entity_registry
                        WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'
                        GROUP BY domain ORDER BY domain""",
                    (scope_type, scope_id),
                )
                domain_rows = cursor.fetchall()
                cursor.execute(
                    f"""SELECT e.id::text,e.domain,e.canonical_name,e.registry_version,
                               COALESCE((SELECT COUNT(*) FROM {self.schema}.chunk_branches b
                                          WHERE b.entity_id=e.id AND b.status='active'),0)
                        FROM {self.schema}.entity_registry e
                        WHERE e.scope_type=%s AND e.scope_id=%s::uuid AND e.status='active'
                        ORDER BY e.domain,e.canonical_name""",
                    (scope_type, scope_id),
                )
                entity_rows = cursor.fetchall()
        nodes: list[dict[str, Any]] = []
        for domain, entity_count in domain_rows:
            nodes.append({
                "node_id": f"domain:{domain}", "node_type": "domain", "domain": domain,
                "entity_id": None, "canonical_name": None, "time_bucket": None,
                "node_key": f"domain:{domain}", "branch_key": None, "parent_id": None,
                "registry_version": version,
                "statistics": {"entity_count": int(entity_count)},
            })
        for entity_id, domain, name, entity_version, mount_count in entity_rows:
            nodes.append({
                "node_id": entity_id, "node_type": "entity", "domain": domain,
                "entity_id": entity_id, "canonical_name": name, "time_bucket": None,
                "node_key": f"entity:{domain}:{entity_id}", "branch_key": None,
                "parent_id": f"domain:{domain}",
                "registry_version": int(entity_version or 1),
                "statistics": {"chunk_count": int(mount_count)},
            })
        return {
            "scope_key": f"{scope_type}:{scope_id}",
            "registry_version": version,
            "nodes": nodes,
        }

    def add_outbox_event(self, event: dict[str, Any]) -> str:
        event_id = str(event.get("event_id") or new_uuid())
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.outbox_events
                    (id,job_id,aggregate_type,aggregate_id,event_type,event_version,schema_version,
                     scope_type,scope_id,trace_id,payload)
                    VALUES (%s::uuid,%s::uuid,%s,%s::uuid,%s,%s,%s,%s,%s::uuid,%s,%s::jsonb)
                    ON CONFLICT DO NOTHING""",
                    (
                        event_id, event.get("job_id"), event.get("aggregate_type", "knowledge_item"),
                        event.get("aggregate_id"), event.get("event_type"), int(event.get("event_version") or 1),
                        int(event.get("schema_version") or 1), event.get("scope_type"),
                        event.get("scope_id"), event.get("trace_id"), _as_json(event.get("payload")),
                    ),
                )
                return event_id

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,job_id::text,aggregate_type,aggregate_id::text,event_type,
                               event_version,schema_version,scope_type,scope_id::text,trace_id,payload
                        FROM {self.schema}.outbox_events
                        WHERE status IN ('pending','failed') AND available_at<=CURRENT_TIMESTAMP
                        ORDER BY created_at LIMIT %s""",
                    (max(1, limit),),
                )
                return [
                    {
                        "event_id": row[0], "job_id": row[1], "aggregate_type": row[2],
                        "aggregate_id": row[3], "event_type": row[4],
                        "event_version": int(row[5]), "schema_version": int(row[6]),
                        "scope_type": row[7], "scope_id": row[8], "trace_id": row[9],
                        "payload": row[10] or {},
                    }
                    for row in cursor.fetchall()
                ]

    def mark_outbox_published(self, event_id: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.outbox_events SET status='published',published_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid""",
                    (event_id,),
                )

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.outbox_events
                        SET status='failed',retry_count=retry_count+1,last_error=%s,
                            available_at=CURRENT_TIMESTAMP + INTERVAL '30 seconds'
                        WHERE id=%s::uuid""",
                    (error[:1000], event_id),
                )

    def record_search(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        query_hash: str,
        query_redacted: str,
        filters: dict[str, Any],
        tree_mode: str,
        execution_path: str,
        diagnostics: dict[str, Any],
        result_count: int,
        duration_ms: int,
        request_id: str,
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.search_history
                    (user_id,scope_type,scope_id,query_hash,query_redacted,filters,tree_mode,
                     execution_path,diagnostics,result_count,duration_ms,request_id)
                    VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s)""",
                    (
                        user_id, scope_type, scope_id, query_hash, query_redacted,
                        _as_json(filters), tree_mode, execution_path, _as_json(diagnostics),
                        result_count, duration_ms, request_id,
                    ),
                )

    def create_qa_conversation(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        title: str | None,
        retrieval_mode: str = "quick",
        knowledge_base_ids: list[str] | None = None,
    ) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.qa_conversations
                    (user_id,scope_type,scope_id,title,retrieval_mode,knowledge_base_ids)
                    VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s::uuid[])
                    RETURNING id::text""",
                    (user_id, scope_type, scope_id, title, retrieval_mode, knowledge_base_ids or []),
                )
                return str(cursor.fetchone()[0])

    def list_qa_conversations(self, *, user_id: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        page, page_size = max(1, page), min(100, max(1, page_size))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT COUNT(*) FROM {self.schema}.qa_conversations
                        WHERE user_id=%s::uuid AND status='active' AND deleted_at IS NULL""",
                    (user_id,),
                )
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""SELECT id::text,title,retrieval_mode,knowledge_base_ids,created_at,updated_at,
                               (SELECT COUNT(*) FROM {self.schema}.qa_messages m WHERE m.conversation_id=c.id)
                        FROM {self.schema}.qa_conversations c
                        WHERE user_id=%s::uuid AND status='active' AND deleted_at IS NULL
                        ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
                    (user_id, page_size, (page - 1) * page_size),
                )
                items = [
                    {
                        "id": row[0], "title": row[1] or "新的对话",
                        "retrieval_mode": row[2] or "quick", "knowledge_base_ids": row[3] or [],
                        "created_at": row[4].isoformat() if row[4] else None,
                        "updated_at": row[5].isoformat() if row[5] else None,
                        "last_message_at": row[5].isoformat() if row[5] else None,
                        "message_count": int(row[6]),
                    }
                    for row in cursor.fetchall()
                ]
                return items, total

    def get_qa_conversation(self, *, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id::text,title,retrieval_mode,knowledge_base_ids,created_at,updated_at,
                               scope_type,scope_id::text
                        FROM {self.schema}.qa_conversations
                        WHERE id=%s::uuid AND user_id=%s::uuid AND status='active' AND deleted_at IS NULL""",
                    (conversation_id, user_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cursor.execute(
                    f"""SELECT id::text,role,content,citations,model_name,prompt_version,status,
                               token_usage,duration_ms,error_message_safe,created_at
                        FROM {self.schema}.qa_messages WHERE conversation_id=%s::uuid ORDER BY created_at""",
                    (conversation_id,),
                )
                messages = [
                    {
                        "id": item[0], "role": item[1], "content": item[2], "citations": item[3] or [],
                        "model_name": item[4], "prompt_version": item[5], "status": item[6],
                        "token_usage": item[7] or {}, "duration_ms": item[8],
                        "error_message": item[9], "created_at": item[10].isoformat() if item[10] else None,
                    }
                    for item in cursor.fetchall()
                ]
                return {
                    "id": row[0], "title": row[1] or "新的对话", "retrieval_mode": row[2] or "quick",
                    "knowledge_base_ids": row[3] or [],
                    "created_at": row[4].isoformat() if row[4] else None,
                    "updated_at": row[5].isoformat() if row[5] else None,
                    "scope_type": row[6],
                    "scope_id": row[7],
                    "messages": messages,
                }

    def rename_qa_conversation(self, *, user_id: str, conversation_id: str, title: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.qa_conversations SET title=%s,updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid AND user_id=%s::uuid AND status='active' AND deleted_at IS NULL""",
                    (title[:300], conversation_id, user_id),
                )
                return cursor.rowcount == 1

    def delete_qa_conversation(self, *, user_id: str, conversation_id: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.qa_conversations
                        SET status='deleted',deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid AND user_id=%s::uuid AND status='active' AND deleted_at IS NULL""",
                    (conversation_id, user_id),
                )
                return cursor.rowcount == 1

    def add_qa_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        citations: list[dict[str, Any]] | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        status: str = "completed",
        token_usage: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        error_message: str | None = None,
    ) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.qa_messages
                    (conversation_id,role,content,citations,model_name,prompt_version,status,
                     token_usage,duration_ms,error_message_safe)
                    VALUES (%s::uuid,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s)
                    RETURNING id::text""",
                    (
                        conversation_id, role, content, _as_json(citations or []), model_name,
                        prompt_version, status, _as_json(token_usage), duration_ms, error_message,
                    ),
                )
                message_id = str(cursor.fetchone()[0])
                cursor.execute(
                    f"UPDATE {self.schema}.qa_conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=%s::uuid",
                    (conversation_id,),
                )
                return message_id

    def update_qa_message(self, message_id: str, **values: Any) -> bool:
        allowed = {
            "content",
            "citations",
            "model_name",
            "prompt_version",
            "status",
            "token_usage",
            "duration_ms",
            "error_code",
            "error_stage",
            "error_class",
            "error_message",
            "retryable",
            "diagnostic_ref",
        }
        updates = [(key, value) for key, value in values.items() if key in allowed]
        if not updates:
            return False
        assignments = []
        params: list[Any] = []
        for key, value in updates:
            column = "error_message_safe" if key == "error_message" else key
            if key in {"citations", "token_usage"}:
                assignments.append(f"{column}=%s::jsonb")
                params.append(_as_json(value))
            else:
                assignments.append(f"{column}=%s")
                params.append(value)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.qa_messages
                        SET {', '.join(assignments)}
                        WHERE id=%s::uuid""",
                    (*params, message_id),
                )
                return cursor.rowcount == 1

    @staticmethod
    def _job_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]), "status": row[1], "current_stage": row[2],
            "lease_owner": row[3], "lease_until": row[4], "lease_epoch": int(row[5] or 0),
            "retry_count": int(row[6] or 0), "next_retry_at": row[7],
            "parse_status": row[8],
        }

    @staticmethod
    def _full_job_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]), "status": row[1], "current_stage": row[2],
            "lease_owner": row[3], "lease_until": row[4], "lease_epoch": int(row[5] or 0),
            "retry_count": int(row[6] or 0), "next_retry_at": row[7], "parse_status": row[8],
            "source_event_id": str(row[9]),
            "knowledge_item_id": str(row[10]), "resource_type": row[11],
            "resource_id": str(row[12]), "knowledge_base_id": str(row[13]),
            "scope_type": row[14], "scope_id": str(row[15]),
            "source_conversation_id": str(row[16]) if row[16] else None,
            "source_audience_policy": row[17], "content_version": int(row[18]),
            "processing_version": row[19], "acl_version": int(row[20] or 0),
            "last_error": row[21],
            "source_payload": row[22] if len(row) > 22 and isinstance(row[22], dict) else {},
        }

    @staticmethod
    def _chunk_row(row: Any) -> Chunk:
        return Chunk(
            chunk_id=row[0], resource_snapshot_id=str(row[1]), knowledge_item_id=str(row[2]),
            resource_type=row[3], resource_id=str(row[4]), knowledge_base_id=str(row[5]),
            scope_type=row[6], scope_id=str(row[7]), content_version=int(row[8]),
            processing_version=row[9], chunking_version=row[10], content_variant=row[11],
            chunk_index=int(row[12]), chunk_count=int(row[13]), title=row[14], file_name=row[15],
            heading_path=tuple(row[16] or ()), context_header=row[17] or {}, content=row[18],
            content_hash=row[19], source_locator=row[20] or {},
            source_conversation_id=str(row[21]) if row[21] else None, conversation_type=row[22],
            document_id=str(row[23]) if row[23] else None, message_id=str(row[24]) if row[24] else None,
            sent_at=row[25].isoformat() if row[25] else None, auth_partition_key=row[26],
            auth_object_key=row[27], acl_version=int(row[28] or 0), sensitivity=row[29],
            embedding_model=row[30], embedding_dimensions=row[31], embedding_status=row[32],
            rag_eligible=bool(row[33]), lifecycle_status=row[34],
            entity_ids=tuple(row[35] or ()),
            entity_mounts=tuple(dict(item) for item in (row[36] or ())),
            registry_version=int(row[37] or 0),
        )

    @staticmethod
    def _candidate_row(row: Any) -> dict[str, Any]:
        return {
            "candidate_id": str(row[0]), "candidate_name": row[1], "candidate_domain": row[2],
            "normalized_key": row[3], "score": float(row[4] or 0), "status": row[5],
            "mention_count": int(row[6] or 0), "distinct_chunk_count": int(row[7] or 0),
            "distinct_source_count": int(row[8] or 0), "distinct_conversation_count": int(row[9] or 0),
            "suggested_entity_id": row[10],
            "first_seen_at": row[11].isoformat() if row[11] else None,
            "last_seen_at": row[12].isoformat() if row[12] else None,
            "resolved_entity_id": row[13] if len(row) > 13 else None,
            "review_note": row[14] if len(row) > 14 else None,
        }


class InMemoryRagMVPRepository:
    """Deterministic repository used by unit tests and local fixtures."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, str] = {}
        self.attempts: list[dict[str, Any]] = []
        self.snapshots: dict[str, ResourceContext] = {}
        self.chunks: dict[str, Chunk] = {}
        self.projections: list[dict[str, Any]] = []
        self.entities: list[dict[str, Any]] = []
        self.aliases: list[dict[str, Any]] = []
        self.branches: dict[tuple[str, str], dict[str, Any]] = {}
        self.scan_watermarks: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.relations: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.candidate_mentions: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.branch_refresh_jobs: dict[str, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []

    def create_or_get_job(self, envelope: dict[str, Any], *, processing_version: str | None = None) -> dict[str, Any]:
        payload = envelope.get("payload") or {}
        event_id = str(envelope.get("event_id") or "")
        digest = payload_hash(payload)
        if event_id in self.events:
            if self.events[event_id] != digest:
                raise ValueError("source_event_id payload changed")
            return next(dict(item) for item in self.jobs.values() if item["source_event_id"] == event_id)
        resource_key = (
            str(payload.get("knowledge_item_id")), str(payload.get("resource_type")),
            str(payload.get("resource_id")), int(payload.get("content_version") or 1),
        )
        for item in self.jobs.values():
            if (
                (item["knowledge_item_id"], item["resource_type"], item["resource_id"], item["content_version"])
                == resource_key
                and item["job_type"] == "full_process"
                and item["status"]
                in {"pending", "processing", "retry_wait", "ready", "metadata_only"}
            ):
                self.events[event_id] = digest
                return dict(item)
        audience = str(payload.get("source_audience_policy") or "")
        scope_type, scope_id = _resolve_job_scope(payload, envelope)
        job_id = new_uuid()
        value = {
            "id": job_id, "source_event_id": event_id, "payload_hash": digest,
            "job_type": "full_process", "knowledge_item_id": resource_key[0],
            "resource_type": resource_key[1], "resource_id": resource_key[2],
            "knowledge_base_id": str(payload.get("knowledge_base_id") or ZERO_UUID),
            "scope_type": scope_type, "scope_id": scope_id,
            "source_conversation_id": payload.get("source_conversation_id"),
            "source_audience_policy": audience or None, "content_version": resource_key[3],
            "processing_version": processing_version or settings.processing_version,
            "acl_version": int(payload.get("acl_version") or 0), "status": "pending",
            "current_stage": None, "lease_owner": None, "lease_until": None, "lease_epoch": 0,
            "parse_status": "pending",
            "retry_count": 0, "next_retry_at": datetime.now(timezone.utc), "last_error": None,
            "source_payload": dict(payload),
        }
        self.jobs[job_id] = value
        self.events[event_id] = digest
        return dict(value)

    def get_job(self, job_id: str | None = None, *, source_event_id: str | None = None) -> dict[str, Any] | None:
        if job_id and job_id in self.jobs:
            return dict(self.jobs[job_id])
        if source_event_id:
            value = next((item for item in self.jobs.values() if item["source_event_id"] == source_event_id), None)
            return dict(value) if value else None
        return None

    def claim_jobs(
        self,
        lane: str,
        *,
        limit: int = 1,
        lease_seconds: int | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        def eligible(item: dict[str, Any]) -> bool:
            if (
                item["status"] in {"pending", "retry_wait", "processing"}
                and item.get("current_stage") in {None, "fetch", "parse", "chunk"}
            ):
                return lane == "parse"
            if item["status"] in {"processing", "retry_wait"} and item.get("current_stage") == "index":
                return lane == "index"
            if item["status"] == "ready" and item.get("current_stage") == "memory":
                return lane == "memory"
            return False

        output = []
        for item in self.jobs.values():
            if len(output) >= limit:
                break
            if job_id and item["id"] != job_id:
                continue
            if not eligible(item):
                continue
            if item.get("lease_until") and item["lease_until"] > datetime.now(timezone.utc):
                continue
            if item["status"] == "pending":
                item["status"] = "processing"
            elif item["status"] == "retry_wait":
                item["status"] = "processing"
            item.setdefault("current_stage", None)
            if not item["current_stage"]:
                item["current_stage"] = "fetch" if lane == "parse" else lane
            item["lease_owner"] = f"{settings.service_name}:{lane}"
            item["lease_epoch"] = int(item.get("lease_epoch") or 0) + 1
            item["lease_until"] = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds or settings.task_lease_seconds)
            output.append(dict(item))
        return output

    def list_recoverable_jobs(
        self,
        lane: str,
        *,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        output = []
        for item in self.jobs.values():
            if len(output) >= limit:
                break
            if lane == "parse":
                eligible = (
                    item["status"] in {"pending", "retry_wait", "processing"}
                    and item.get("current_stage") in {None, "fetch", "parse", "chunk"}
                )
            elif lane == "index":
                eligible = (
                    item["status"] in {"processing", "retry_wait"}
                    and item.get("current_stage") == "index"
                )
            else:
                eligible = (
                    item["status"] == "ready"
                    and item.get("current_stage") == "memory"
                )
            if not eligible:
                continue
            if item.get("lease_until") and item["lease_until"] > now:
                continue
            if item.get("next_retry_at") and item["next_retry_at"] > now:
                continue
            output.append(dict(item))
        return output

    def heartbeat(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        lease_seconds: int | None = None,
    ) -> bool:
        item = self.jobs.get(job_id)
        if (
            not item
            or item.get("lease_owner") != owner
            or int(item.get("lease_epoch") or 0) != int(epoch)
        ):
            return False
        item["lease_until"] = datetime.now(timezone.utc) + timedelta(
            seconds=lease_seconds or settings.task_lease_seconds
        )
        return True

    def update_job(self, job_id: str, **fields: Any) -> None:
        current = self.jobs[job_id]
        if "status" in fields:
            validate_job_transition(str(current["status"]), str(fields["status"]))
        current.update(fields)

    def update_job_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        item = self.jobs.get(job_id)
        if (
            not item
            or item.get("lease_owner") != owner
            or int(item.get("lease_epoch") or 0) != int(epoch)
        ):
            return False
        if "status" in fields:
            validate_job_transition(str(item["status"]), str(fields["status"]))
        item.update(fields)
        return True

    def complete_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        return self.update_job_if_owned(
            job_id,
            owner=owner,
            epoch=epoch,
            fields={**fields, "status": fields.get("status", "ready")},
        )

    def fail_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool:
        return self.update_job_if_owned(
            job_id,
            owner=owner,
            epoch=epoch,
            fields={**fields, "status": "failed"},
        )

    def add_attempt(
        self,
        job_id: str,
        *,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
        **fields: Any,
    ) -> str:
        job = self.jobs.get(job_id)
        if (
            lease_owner is not None
            and lease_epoch is not None
            and (
                not job
                or job.get("lease_owner") != lease_owner
                or int(job.get("lease_epoch") or 0) != int(lease_epoch)
            )
        ):
            raise RuntimeError("lease lost")
        value = {"id": new_uuid(), "job_id": job_id, **fields}
        if lease_owner is not None:
            value["lease_owner"] = lease_owner
        if lease_epoch is not None:
            value["lease_epoch"] = int(lease_epoch)
        self.attempts.append(value)
        return value["id"]

    def upsert_snapshot(self, context: ResourceContext) -> str:
        existing = next(
            (
                snapshot_id for snapshot_id, value in self.snapshots.items()
                if value.knowledge_item_id == context.knowledge_item_id
                and value.content_version == context.content_version
            ),
            None,
        )
        snapshot_id = existing or new_uuid()
        self.snapshots[snapshot_id] = context
        return snapshot_id

    def upsert_chunks(self, chunks: list[Chunk]) -> int:
        for chunk in chunks:
            self.chunks[chunk.chunk_id] = chunk
        return len(chunks)

    def list_chunks(self, **filters: Any) -> list[Chunk]:
        values = list(self.chunks.values())
        if filters.get("snapshot_id"):
            values = [item for item in values if item.resource_snapshot_id == filters["snapshot_id"]]
        if filters.get("chunk_ids"):
            values = [item for item in values if item.chunk_id in set(filters["chunk_ids"])]
        if filters.get("variant"):
            values = [item for item in values if item.content_variant == filters["variant"]]
        if filters.get("embedding_status"):
            values = [item for item in values if item.embedding_status == filters["embedding_status"]]
        if filters.get("scope_type"):
            values = [item for item in values if item.scope_type == filters["scope_type"]]
        if filters.get("scope_id"):
            values = [item for item in values if item.scope_id == filters["scope_id"]]
        output = [item for item in sorted(values, key=lambda value: value.chunk_index)]
        for chunk in output:
            active = [
                value for (chunk_id, _), value in self.branches.items()
                if chunk_id == chunk.chunk_id and value.get("status") == "active"
            ]
            ordered = sorted(active, key=lambda value: value["entity_id"])
            chunk.entity_ids = tuple(value["entity_id"] for value in ordered)
            chunk.entity_mounts = tuple(
                {
                    "entity_id": value["entity_id"],
                    "domain": value.get("domain", ""),
                    "confidence": float(value.get("confidence", 1.0)),
                    "method": value.get("mount_method", "explicit"),
                }
                for value in ordered
            )
            chunk.registry_version = max(
                (int(value.get("registry_version") or 0) for value in active),
                default=0,
            )
        return output

    def list_scan_conversations(self, *, limit: int = 20) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], list[Chunk]] = defaultdict(list)
        for chunk in self.chunks.values():
            if chunk.resource_type != "message" or not chunk.source_conversation_id:
                continue
            if chunk.lifecycle_status != "active" or not chunk.sent_at:
                continue
            groups[(chunk.scope_type, chunk.scope_id, chunk.source_conversation_id)].append(chunk)
        output: list[dict[str, Any]] = []
        for (scope_type, scope_id, conversation_id), chunks in groups.items():
            watermark = self.scan_watermarks.get((scope_type, scope_id, conversation_id))
            after = watermark["last_sent_at"] if watermark else None
            pending = [c for c in chunks if not after or (c.sent_at or "") > after]
            if not pending:
                continue
            output.append({
                "scope_type": scope_type, "scope_id": scope_id,
                "conversation_id": conversation_id, "pending": len(pending),
                "last_sent_at": min(c.sent_at for c in pending if c.sent_at),
            })
        output.sort(key=lambda item: item["last_sent_at"] or "")
        return output[: max(1, int(limit))]

    def get_scan_watermark(
        self, *, scope_type: str, scope_id: str, conversation_id: str
    ) -> str | None:
        value = self.scan_watermarks.get((scope_type, scope_id, conversation_id))
        return value["last_sent_at"] if value else None

    def list_conversation_chunks(
        self,
        *,
        scope_type: str,
        scope_id: str,
        conversation_id: str,
        after_sent_at: str | None = None,
        limit: int = 500,
    ) -> list[Chunk]:
        values = [
            chunk for chunk in self.chunks.values()
            if chunk.scope_type == scope_type
            and chunk.scope_id == scope_id
            and chunk.source_conversation_id == conversation_id
            and chunk.resource_type == "message"
            and chunk.lifecycle_status == "active"
            and chunk.sent_at
            and (after_sent_at is None or chunk.sent_at > after_sent_at)
        ]
        values.sort(key=lambda item: (item.sent_at or "", item.chunk_id))
        return values[: max(1, int(limit))]

    def set_scan_watermark(
        self,
        *,
        scope_type: str,
        scope_id: str,
        conversation_id: str,
        last_sent_at: str,
        last_chunk_id: str | None = None,
        window_count: int = 0,
    ) -> None:
        key = (scope_type, scope_id, conversation_id)
        current = self.scan_watermarks.get(key)
        if current and str(current["last_sent_at"]) >= str(last_sent_at):
            return
        self.scan_watermarks[key] = {
            "last_sent_at": last_sent_at,
            "last_chunk_id": last_chunk_id,
            "window_count": (current["window_count"] if current else 0) + int(window_count),
        }

    def find_entities_by_normalized(
        self, *, scope_type: str, scope_id: str, normalized_keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        keys = {key for key in normalized_keys if key}
        if not keys:
            return {}
        found: dict[str, dict[str, Any]] = {}
        for item in self.entities:
            if item["scope_type"] != scope_type or item["scope_id"] != scope_id:
                continue
            if item.get("status") != "active" or item["normalized_key"] not in keys:
                continue
            found[item["normalized_key"]] = {
                "entity_id": item["id"], "domain": item["domain"],
                "canonical_name": item["canonical_name"],
                "registry_version": int(item.get("registry_version") or 1),
            }
        for alias in self.aliases:
            if alias.get("scope_type") != scope_type or alias.get("scope_id") != scope_id:
                continue
            if alias.get("status", "active") != "active":
                continue
            key = alias.get("normalized_alias")
            if key not in keys or key in found:
                continue
            entity = next((e for e in self.entities if e["id"] == alias["entity_id"]), None)
            if entity is None or entity.get("status") != "active":
                continue
            found[key] = {
                "entity_id": entity["id"], "domain": entity["domain"],
                "canonical_name": entity["canonical_name"],
                "registry_version": int(entity.get("registry_version") or 1),
            }
        return found

    def upsert_entity_relation(
        self,
        *,
        scope_type: str,
        scope_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        confidence: float,
        evidence_chunk_ids: list[str] | None = None,
    ) -> None:
        # Self-loops carry no information and would pollute graph expansion.
        if source_entity_id == target_entity_id:
            return
        key = (scope_type, scope_id, source_entity_id, target_entity_id, relation_type)
        current = self.relations.get(key)
        evidence = list(dict.fromkeys(evidence_chunk_ids or []))
        self.relations[key] = {
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "relation_type": relation_type,
            "confidence": max(float(confidence), float(current["confidence"])) if current else float(confidence),
            "evidence_chunk_ids": list(dict.fromkeys((current["evidence_chunk_ids"] if current else []) + evidence)),
        }

    def find_related_entities(
        self,
        *,
        scope_type: str,
        scope_id: str,
        entity_ids: list[str],
        relation_types: list[str] | None = None,
        direction: str = "both",
        min_confidence: float = 0.7,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        seeds = {value for value in entity_ids if value}
        if not seeds:
            return []
        wanted = set(relation_types) if relation_types else None
        output: list[dict[str, Any]] = []
        for key, value in self.relations.items():
            if key[0] != scope_type or key[1] != scope_id:
                continue
            if float(value["confidence"]) < float(min_confidence):
                continue
            if wanted and value["relation_type"] not in wanted:
                continue
            source = value["source_entity_id"]
            target = value["target_entity_id"]
            if direction == "outbound" and source not in seeds:
                continue
            if direction == "inbound" and target not in seeds:
                continue
            if direction == "both" and not ({source, target} & seeds):
                continue
            neighbor_id = target if direction != "inbound" else source
            if direction == "both":
                neighbor_id = target if source in seeds else source
            neighbor = next((e for e in self.entities if e["id"] == neighbor_id), None)
            if neighbor is None or neighbor.get("status") != "active":
                continue
            output.append({
                "entity_id": neighbor_id, "domain": neighbor["domain"],
                "canonical_name": neighbor["canonical_name"],
                "relation_type": value["relation_type"],
                "confidence": float(value["confidence"]),
                "source_entity_id": source, "target_entity_id": target,
            })
        output.sort(key=lambda item: (-item["confidence"], item["canonical_name"]))
        return output[: max(1, int(limit))]

    def list_pending_branch_refresh_jobs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return [
            dict(value) for value in self.branch_refresh_jobs.values()
            if value["status"] in {"pending", "failed"}
        ][:limit]

    def get_branch_refresh_job(self, job_id: str) -> dict[str, Any] | None:
        value = self.branch_refresh_jobs.get(job_id)
        return dict(value) if value else None

    def mark_branch_refresh(
        self,
        job_id: str,
        *,
        status: str,
        processed_count: int,
        error: str | None = None,
    ) -> None:
        value = self.branch_refresh_jobs[job_id]
        value["status"] = status
        value["processed_count"] = processed_count
        value["last_error"] = error

    def update_embedding_status(
        self,
        chunk_ids: list[str],
        *,
        status: str,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        for chunk_id in chunk_ids:
            chunk = self.chunks[chunk_id]
            chunk.embedding_status = status
            chunk.embedding_model = model or chunk.embedding_model
            chunk.embedding_dimensions = dimensions or chunk.embedding_dimensions

    def mark_older_chunks_inactive(self, *, knowledge_item_id: str, content_version: int) -> None:
        for chunk in self.chunks.values():
            if chunk.knowledge_item_id == knowledge_item_id and chunk.content_version < content_version:
                chunk.lifecycle_status = "inactive"

    def delete_resource_data(self, *, knowledge_item_id: str, resource_id: str) -> int:
        removed = {
            chunk_id for chunk_id, chunk in self.chunks.items()
            if chunk.knowledge_item_id == knowledge_item_id or chunk.resource_id == resource_id
        }
        for chunk_id in removed:
            del self.chunks[chunk_id]
        self.projections = [item for item in self.projections if item.get("chunk_id") not in removed]
        for key in list(self.branches):
            if key[0] in removed:
                del self.branches[key]
        return len(removed)


    def upsert_projection(self, chunk: Chunk, **value: Any) -> None:
        existing = next(
            (
                item
                for item in self.projections
                if item["chunk_id"] == chunk.chunk_id
                and item["es_index_alias"] == value.get("alias")
                and item["mapping_version"] == value.get("mapping_version")
            ),
            None,
        )
        record = {"chunk_id": chunk.chunk_id, "retry_count": 0, **value}
        if existing:
            existing.update(record)
        else:
            self.projections.append(record)

    def ensure_projection_records(
        self,
        chunks: list[Chunk],
        *,
        mapping_version: str,
    ) -> dict[str, dict[str, Any]]:
        for chunk in chunks:
            alias = (
                settings.elasticsearch_protected_write_index
                if chunk.protected
                else settings.elasticsearch_display_write_index
            )
            existing = next(
                (
                    item
                    for item in self.projections
                    if item["chunk_id"] == chunk.chunk_id
                    and item["es_index_alias"] == alias
                    and item["mapping_version"] == mapping_version
                ),
                None,
            )
            if not existing:
                self.projections.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "chunk_variant": chunk.content_variant,
                        "es_index_alias": alias,
                        "es_document_id": chunk.chunk_id,
                        "mapping_version": mapping_version,
                        "status": "pending",
                        "retry_count": 0,
                        "next_retry_at": datetime.now(timezone.utc),
                        "last_error": None,
                        "failure_stage": None,
                    }
                )
        return {
            item["chunk_id"]: dict(item)
            for item in self.projections
            if item["chunk_id"] in {chunk.chunk_id for chunk in chunks}
        }

    def list_projection_records(
        self,
        *,
        knowledge_item_id: str,
        content_version: int,
    ) -> list[dict[str, Any]]:
        chunk_ids = {
            chunk.chunk_id
            for chunk in self.chunks.values()
            if chunk.knowledge_item_id == knowledge_item_id
            and chunk.content_version == int(content_version)
            and chunk.lifecycle_status == "active"
        }
        return [
            dict(item)
            for item in self.projections
            if item["chunk_id"] in chunk_ids
        ]

    def update_projection_status(
        self,
        chunk_ids: list[str],
        *,
        status: str,
        failure_stage: str | None = None,
        error: str | None = None,
        increment_retry: bool = False,
        next_retry_at: datetime | None = None,
    ) -> int:
        updated = 0
        for item in self.projections:
            if item["chunk_id"] not in chunk_ids:
                continue
            validate_projection_transition(str(item["status"]), status)
            item["status"] = status
            item["failure_stage"] = failure_stage
            item["last_error"] = error
            if increment_retry:
                item["retry_count"] = int(item.get("retry_count") or 0) + 1
            item["next_retry_at"] = next_retry_at
            if status == "ready":
                item["indexed_at"] = datetime.now(timezone.utc)
            updated += 1
        return updated

    def load_entity_registry(self, *, scope_type: str, scope_id: str) -> tuple[list[Entity], list[EntityAlias], int]:
        entities = [
            Entity(**{key: item[key] for key in Entity.__dataclass_fields__ if key in item})
            for item in self.entities
            if item["scope_type"] == scope_type and item["scope_id"] == scope_id and item["status"] == "active"
        ]
        aliases = [
            EntityAlias(**{key: item[key] for key in EntityAlias.__dataclass_fields__ if key in item})
            for item in self.aliases
            if item["scope_type"] == scope_type and item["scope_id"] == scope_id and item["status"] == "active"
        ]
        version = max((int(item.get("registry_version") or 1) for item in self.entities), default=1)
        return entities, aliases, version

    def locate_entities_exact(
        self, *, scope_type: str, scope_id: str, normalized: str
    ) -> list[dict[str, Any]]:
        if not normalized:
            return []
        found: dict[str, dict[str, Any]] = {}
        for item in self.entities:
            if item["scope_type"] != scope_type or item["scope_id"] != scope_id:
                continue
            if item.get("status") != "active" or item["normalized_key"] != normalized:
                continue
            found[item["id"]] = {
                "entity_id": item["id"], "domain": item["domain"],
                "canonical_name": item["canonical_name"],
                "registry_version": int(item.get("registry_version") or 1),
                "match_method": "exact", "match_score": 0.95,
            }
        for alias in self.aliases:
            if alias.get("scope_type") != scope_type or alias.get("scope_id") != scope_id:
                continue
            if alias.get("status", "active") != "active":
                continue
            if alias.get("normalized_alias") != normalized:
                continue
            entity = next((e for e in self.entities if e["id"] == alias["entity_id"]), None)
            if entity is None or entity["id"] in found:
                continue
            found[entity["id"]] = {
                "entity_id": entity["id"], "domain": entity["domain"],
                "canonical_name": entity["canonical_name"],
                "registry_version": int(entity.get("registry_version") or 1),
                "match_method": "alias", "match_score": 0.93,
            }
        return list(found.values())

    def locate_entities_fuzzy(
        self, *, scope_type: str, scope_id: str, normalized: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        if not normalized:
            return []
        scored: dict[str, dict[str, Any]] = {}

        def consider(entity: dict[str, Any], text: str) -> None:
            ratio = _similarity_ratio(normalized, text)
            if ratio >= 0.8:
                method, score = "fuzzy", min(0.85, 0.8 + (ratio - 0.8) * 0.25)
            elif len(normalized) >= 2 and normalized in text:
                # Containment is useful but must be verified downstream, so it
                # carries its own method rather than masquerading as a fuzzy hit.
                method, score = "substring", 0.70
            else:
                return
            current = scored.get(entity["id"])
            if current is None or score > current["match_score"]:
                scored[entity["id"]] = {
                    "entity_id": entity["id"], "domain": entity["domain"],
                    "canonical_name": entity["canonical_name"],
                    "registry_version": int(entity.get("registry_version") or 1),
                    "match_method": method, "match_score": round(score, 4),
                }

        for item in self.entities:
            if item["scope_type"] != scope_type or item["scope_id"] != scope_id:
                continue
            if item.get("status") == "active":
                consider(item, item["normalized_key"])
        for alias in self.aliases:
            if alias.get("scope_type") != scope_type or alias.get("scope_id") != scope_id:
                continue
            if alias.get("status", "active") != "active":
                continue
            entity = next((e for e in self.entities if e["id"] == alias["entity_id"]), None)
            if entity is not None and entity.get("status") == "active":
                consider(entity, alias.get("normalized_alias", ""))
        ranked = sorted(scored.values(), key=lambda item: (-item["match_score"], item["entity_id"]))
        return ranked[: max(1, int(limit))]

    def locate_entities_semantic(
        self,
        *,
        scope_type: str,
        scope_id: str,
        embedding: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not embedding:
            return []
        output: list[dict[str, Any]] = []
        for item in self.entities:
            if item["scope_type"] != scope_type or item["scope_id"] != scope_id:
                continue
            if item.get("status") != "active":
                continue
            vector = item.get("embedding")
            if not vector:
                continue
            output.append({
                "entity_id": item["id"], "domain": item["domain"],
                "canonical_name": item["canonical_name"],
                "registry_version": int(item.get("registry_version") or 1),
                "match_method": "semantic",
                "match_score": round(_cosine_similarity(embedding, vector), 4),
            })
        output.sort(key=lambda value: (-value["match_score"], value["entity_id"]))
        return output[: max(1, int(limit))]

    def list_entities_pending_embedding(self, *, limit: int = 50) -> list[dict[str, Any]]:
        pending = [
            {
                "entity_id": item["id"], "scope_type": item["scope_type"],
                "scope_id": item["scope_id"], "domain": item["domain"],
                "canonical_name": item["canonical_name"],
                "description": item.get("description", ""),
                "keywords": " ".join(item.get("keywords") or ()),
                "aliases": " ".join(
                    alias.get("display_alias", "")
                    for alias in self.aliases
                    if alias.get("entity_id") == item["id"]
                ),
            }
            for item in self.entities
            if item.get("status") == "active"
            and item.get("embedding_status", "pending") in {"pending", "failed"}
        ]
        return pending[: max(1, int(limit))]

    def update_entity_embedding(
        self,
        *,
        entity_id: str,
        embedding: list[float],
        model: str,
        dimensions: int,
        status: str = "ready",
    ) -> None:
        for item in self.entities:
            if item["id"] != entity_id:
                continue
            item["embedding"] = list(embedding)
            item["embedding_model"] = model
            item["embedding_dimensions"] = int(dimensions)
            item["embedding_status"] = status

    def upsert_entity(
        self,
        *,
        entity_id: str | None = None,
        scope_type: str,
        scope_id: str,
        domain: str,
        canonical_name: str,
        normalized_key: str,
        registry_version: int = 1,
    ) -> dict[str, Any]:
        entity_id = entity_id or new_uuid()
        value = {
            "id": entity_id, "scope_type": scope_type, "scope_id": scope_id, "domain": domain,
            "canonical_name": canonical_name, "normalized_key": normalized_key, "status": "active",
            "registry_version": registry_version,
        }
        self.entities = [item for item in self.entities if item["id"] != entity_id] + [value]
        return dict(value)

    def upsert_alias(self, **value: Any) -> None:
        self.aliases = [item for item in self.aliases if item["id"] != value["id"]] + [value]

    def list_entities(self, *, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        return [
            {
                "entity_id": item["id"], "domain": item["domain"],
                "canonical_name": item["canonical_name"], "normalized_key": item["normalized_key"],
                "status": item["status"], "registry_version": item["registry_version"],
                "merged_into_entity_id": item.get("merged_into_entity_id"),
            }
            for item in self.entities
            if item["scope_type"] == scope_type and item["scope_id"] == scope_id
        ]

    def get_entity(self, *, scope_type: str, scope_id: str, entity_id: str) -> dict[str, Any] | None:
        value = next(
            (
                item for item in self.list_entities(scope_type=scope_type, scope_id=scope_id)
                if item["entity_id"] == entity_id
            ),
            None,
        )
        if not value:
            return None
        value["aliases"] = [
            dict(item) for item in self.aliases if item["entity_id"] == entity_id
        ]
        return value

    def replace_chunk_mounts(self, chunk: Chunk, mounts: list[EntityMount]) -> None:
        for key in list(self.branches):
            if key[0] == chunk.chunk_id:
                self.branches.pop(key)
        self._upsert_mount_rows(chunk, mounts)

    def merge_chunk_mounts(self, chunk: Chunk, mounts: list[EntityMount]) -> None:
        self._upsert_mount_rows(chunk, mounts)

    def _upsert_mount_rows(self, chunk: Chunk, mounts: list[EntityMount]) -> None:
        rank = {"llm_infer": 0, "window_batch": 1, "explicit": 2}
        for mount in mounts:
            key = (chunk.chunk_id, mount.entity_id)
            current = self.branches.get(key)
            if current is None:
                self.branches[key] = {
                    "entity_id": mount.entity_id,
                    "domain": mount.domain,
                    "registry_version": mount.registry_version,
                    "mount_method": mount.mount_method,
                    "confidence": mount.confidence,
                    "status": "active",
                }
                continue
            # Same rules as Postgres: confidence only moves up, and the stronger
            # channel wins regardless of arrival order.
            current["confidence"] = max(
                float(current.get("confidence", 0)), float(mount.confidence)
            )
            if rank.get(mount.mount_method, 0) > rank.get(current.get("mount_method"), 0):
                current["mount_method"] = mount.mount_method
            current["registry_version"] = mount.registry_version
            current["status"] = "active"

    def upsert_candidate_mention(
        self,
        *,
        scope_type: str,
        scope_id: str,
        candidate_name: str,
        normalized_key: str,
        domain: str,
        chunk: Chunk,
        context_excerpt: str,
        confidence: float = 0.5,
        method: str = "regex",
    ) -> str:
        key = (scope_type, scope_id, domain, normalized_key)
        candidate_id = next(
            (item["id"] for item in self.candidates.values()
             if (item["scope_type"], item["scope_id"], item["candidate_domain"], item["normalized_key"]) == key),
            None,
        )
        if not candidate_id:
            candidate_id = new_uuid()
            self.candidates[candidate_id] = {
                "id": candidate_id, "scope_type": scope_type, "scope_id": scope_id,
                "candidate_name": candidate_name, "normalized_key": normalized_key,
                "candidate_domain": domain, "mention_count": 0, "distinct_chunk_count": 0,
                "distinct_source_count": 0, "distinct_conversation_count": 0,
                "sample_context": context_excerpt, "score": float(confidence), "status": "new",
                "suggested_entity_id": None, "resolved_entity_id": None,
            }
        value = self.candidates[candidate_id]
        # Score tracks the best evidence seen, so the review page can rank by it
        # instead of showing the same number for every candidate.
        value["score"] = max(float(value.get("score") or 0), float(confidence))
        value["mention_count"] += 1
        value["distinct_chunk_count"] = len({item["chunk_id"] for item in self.candidate_mentions if item["candidate_id"] == candidate_id} | {chunk.chunk_id})
        value["distinct_source_count"] = len({self.chunks[item["chunk_id"]].resource_id for item in self.candidate_mentions if item["candidate_id"] == candidate_id} | {chunk.resource_id})
        value["distinct_conversation_count"] = len({self.chunks[item["chunk_id"]].source_conversation_id for item in self.candidate_mentions if item["candidate_id"] == candidate_id} | {chunk.source_conversation_id})
        self.candidate_mentions.append({
            "id": new_uuid(), "candidate_id": candidate_id, "chunk_id": chunk.chunk_id,
            "surface_form": candidate_name, "candidate_domain": domain,
            "context_excerpt": context_excerpt, "confidence": confidence,
            "extraction_method": method, "created_at": datetime.now(timezone.utc),
        })
        return candidate_id

    def list_candidates(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        values = [
            dict(item) for item in self.candidates.values()
            if item["scope_type"] == filters["scope_type"]
            and item["scope_id"] == filters["scope_id"]
            and (not filters.get("status") or item["status"] == filters["status"])
            and (not filters.get("domain") or item["candidate_domain"] == filters["domain"])
            and (not filters.get("query") or filters["query"] in item["candidate_name"])
        ]
        values.sort(key=lambda item: (-item["score"], item["candidate_name"]))
        page, page_size = max(1, filters.get("page", 1)), max(1, filters.get("page_size", 20))
        return values[(page - 1) * page_size: page * page_size], len(values)

    def get_candidate(self, **filters: Any) -> dict[str, Any] | None:
        value = self.candidates.get(filters["candidate_id"])
        if not value or value["scope_type"] != filters["scope_type"] or value["scope_id"] != filters["scope_id"]:
            return None
        return {
            **value,
            "mentions": [
                dict(item) for item in self.candidate_mentions if item["candidate_id"] == value["id"]
            ],
        }

    def review_candidate(self, **value: Any) -> dict[str, Any]:
        candidate = self.candidates[value["candidate_id"]]
        key = (value["candidate_id"], value["review_request_id"])
        existing = next((item for item in self.reviews if (item["candidate_id"], item["review_request_id"]) == key), None)
        if existing:
            return {**existing, "idempotent": True}
        if value.get("expected_status") and candidate["status"] != value["expected_status"]:
            raise ValueError("candidate status changed")
        resolved = None
        version = None
        if value["action"] == "promote":
            resolved = new_uuid()
            version = max((item.get("registry_version", 1) for item in self.entities), default=0) + 1
            self.upsert_entity(
                entity_id=resolved, scope_type=candidate["scope_type"], scope_id=candidate["scope_id"],
                domain=value.get("domain") or candidate["candidate_domain"],
                canonical_name=value.get("canonical_name") or candidate["candidate_name"],
                normalized_key=candidate["normalized_key"], registry_version=version,
            )
        elif value["action"] == "merge":
            resolved = value["target_entity_id"]
            target = next(
                (
                    item
                    for item in self.entities
                    if item["id"] == resolved
                    and item["scope_type"] == candidate["scope_type"]
                    and item["scope_id"] == candidate["scope_id"]
                ),
                None,
            )
            if not target:
                raise LookupError("target entity not found")
            version = target["registry_version"]
            existing_alias = next(
                (
                    item
                    for item in self.aliases
                    if item["scope_type"] == candidate["scope_type"]
                    and item["scope_id"] == candidate["scope_id"]
                    and item["domain"] == candidate["candidate_domain"]
                    and item["normalized_alias"] == candidate["normalized_key"]
                ),
                None,
            )
            if existing_alias:
                existing_alias.update(
                    {
                        "entity_id": resolved,
                        "display_alias": candidate["candidate_name"],
                        "source": "candidate_merge",
                        "status": "active",
                    }
                )
            else:
                self.aliases.append(
                    {
                        "id": new_uuid(),
                        "entity_id": resolved,
                        "scope_type": candidate["scope_type"],
                        "scope_id": candidate["scope_id"],
                        "domain": candidate["candidate_domain"],
                        "display_alias": candidate["candidate_name"],
                        "normalized_alias": candidate["normalized_key"],
                        "source": "candidate_merge",
                        "status": "active",
                    }
                )
        status = {"promote": "promoted", "merge": "merged", "ignore": "ignored", "defer": "deferred"}[value["action"]]
        candidate["status"] = status
        candidate["resolved_entity_id"] = resolved
        refresh_job_id = new_uuid() if resolved else None
        if refresh_job_id:
            self.branch_refresh_jobs[refresh_job_id] = {
                "id": refresh_job_id, "scope_type": candidate["scope_type"],
                "scope_id": candidate["scope_id"], "entity_id": resolved,
                "registry_version": version or 1, "status": "pending",
                "target_count": 0, "processed_count": 0,
            }
        review = {
            "candidate_id": value["candidate_id"], "status": status,
            "resolved_entity_id": resolved, "registry_version": version,
            "branch_refresh_job_id": refresh_job_id,
        }
        self.reviews.append({**value, **review})
        return review

    def get_tree(self, *, scope_type: str, scope_id: str) -> dict[str, Any]:
        prefix = f"{scope_type}:{scope_id}"
        scoped = [
            item for item in self.entities
            if item["scope_type"] == scope_type and item["scope_id"] == scope_id
        ]
        active = [item for item in scoped if item.get("status") == "active"]
        version = max(
            (int(item.get("registry_version") or 1) for item in scoped), default=1
        )
        counts: dict[str, int] = {}
        for item in active:
            counts[item["domain"]] = counts.get(item["domain"], 0) + 1
        nodes: list[dict[str, Any]] = []
        for domain, entity_count in sorted(counts.items()):
            nodes.append({
                "node_id": f"domain:{domain}", "node_type": "domain", "domain": domain,
                "entity_id": None, "canonical_name": None, "time_bucket": None,
                "node_key": f"domain:{domain}", "branch_key": None, "parent_id": None,
                "registry_version": version,
                "statistics": {"entity_count": entity_count},
            })
        for item in sorted(active, key=lambda v: (v["domain"], v.get("canonical_name") or "")):
            mount_count = sum(
                1 for (_, entity_id), value in self.branches.items()
                if entity_id == item["id"] and value.get("status") == "active"
            )
            nodes.append({
                "node_id": item["id"], "node_type": "entity", "domain": item["domain"],
                "entity_id": item["id"], "canonical_name": item["canonical_name"],
                "time_bucket": None, "node_key": f"entity:{item['domain']}:{item['id']}",
                "branch_key": None, "parent_id": f"domain:{item['domain']}",
                "registry_version": int(item.get("registry_version") or 1),
                "statistics": {"chunk_count": mount_count},
            })
        return {"scope_key": prefix, "registry_version": version, "nodes": nodes}

    def tree_metrics(self, *, scope_type: str, scope_id: str) -> dict[str, Any]:
        messages = [
            chunk for chunk in self.chunks.values()
            if chunk.scope_type == scope_type and chunk.scope_id == scope_id
            and chunk.resource_type == "message" and chunk.lifecycle_status == "active"
        ]
        message_ids = {chunk.chunk_id for chunk in messages}
        active = [
            (chunk_id, value) for (chunk_id, _), value in self.branches.items()
            if value.get("status") == "active" and chunk_id in message_ids
        ]
        entities = [
            item for item in self.entities
            if item["scope_type"] == scope_type and item["scope_id"] == scope_id
            and item.get("status") == "active"
        ]
        methods: dict[str, dict[str, Any]] = {}
        for _, value in active:
            method = str(value.get("mount_method") or "explicit")
            entry = methods.setdefault(method, {"count": 0, "total": 0.0, "min": 1.0})
            confidence = float(value.get("confidence") or 0)
            entry["count"] += 1
            entry["total"] += confidence
            entry["min"] = min(entry["min"], confidence)
        mounted = len({chunk_id for chunk_id, _ in active})
        pending = [
            item for item in self.candidates.values()
            if item["scope_type"] == scope_type and item["scope_id"] == scope_id
            and item.get("status") in {"new", "review_ready"}
        ]
        domains: dict[str, int] = {}
        for item in entities:
            domains[item["domain"]] = domains.get(item["domain"], 0) + 1
        relations = {
            key: value for key, value in self.relations.items()
            if key[0] == scope_type and key[1] == scope_id
        }
        relation_types: dict[str, int] = {}
        for value in relations.values():
            key = str(value["relation_type"])
            relation_types[key] = relation_types.get(key, 0) + 1
        return {
            "message_count": len(messages),
            "mounted_chunk_count": mounted,
            "mount_count": len(active),
            "entity_count": len(entities),
            "entities_missing_embedding": sum(
                1 for item in entities if item.get("embedding_status", "pending") != "ready"
            ),
            "pending_candidate_count": len(pending),
            "relation_count": len(relations),
            "scanned_conversation_count": sum(
                1 for key in self.scan_watermarks if key[0] == scope_type and key[1] == scope_id
            ),
            "mount_coverage": round(mounted / len(messages), 4) if messages else 0.0,
            "mount_methods": {
                name: {
                    "count": entry["count"],
                    "min_confidence": round(entry["min"], 4),
                    "avg_confidence": round(entry["total"] / entry["count"], 4),
                }
                for name, entry in methods.items()
            },
            "entities_by_domain": domains,
            "relations_by_type": relation_types,
        }

    def add_outbox_event(self, event: dict[str, Any]) -> str:
        event_id = str(event.get("event_id") or new_uuid())
        existing = next(
            (
                item for item in self.outbox
                if item["aggregate_type"] == event.get("aggregate_type", "knowledge_item")
                and item["aggregate_id"] == event.get("aggregate_id")
                and item["event_type"] == event.get("event_type")
                and item["event_version"] == int(event.get("event_version") or 1)
            ),
            None,
        )
        if existing:
            return existing["event_id"]
        self.outbox.append({"event_id": event_id, "status": "pending", **event})
        return event_id

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(item) for item in self.outbox if item["status"] in {"pending", "failed"}][:limit]

    def mark_outbox_published(self, event_id: str) -> None:
        next(item for item in self.outbox if item["event_id"] == event_id)["status"] = "published"

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        value = next(item for item in self.outbox if item["event_id"] == event_id)
        value["status"] = "failed"
        value["last_error"] = error

    def record_search(self, **value: Any) -> None:
        self.searches.append(dict(value))

    def create_qa_conversation(self, **value: Any) -> str:
        conversation_id = new_uuid()
        self.conversations[conversation_id] = {
            "id": conversation_id, **value, "status": "active", "deleted_at": None,
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        }
        return conversation_id

    def add_qa_message(self, **value: Any) -> str:
        message_id = new_uuid()
        self.messages.append({"id": message_id, **value, "created_at": datetime.now(timezone.utc)})
        conversation = self.conversations.get(value["conversation_id"])
        if conversation:
            conversation["updated_at"] = datetime.now(timezone.utc)
        return message_id

    def update_qa_message(self, message_id: str, **values: Any) -> bool:
        message = next(
            (item for item in self.messages if item["id"] == message_id),
            None,
        )
        if not message:
            return False
        message.update(values)
        conversation = self.conversations.get(message["conversation_id"])
        if conversation:
            conversation["updated_at"] = datetime.now(timezone.utc)
        return True

    def list_qa_conversations(self, *, user_id: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        values = [
            item for item in self.conversations.values()
            if item["user_id"] == user_id and item["status"] == "active"
        ]
        values.sort(key=lambda item: item["updated_at"], reverse=True)
        start = (max(1, page) - 1) * max(1, page_size)
        output = [
            {
                **item,
                "message_count": sum(1 for message in self.messages if message["conversation_id"] == item["id"]),
            }
            for item in values[start:start + max(1, page_size)]
        ]
        return output, len(values)

    def get_qa_conversation(self, *, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        value = self.conversations.get(conversation_id)
        if not value or value["user_id"] != user_id or value["status"] != "active":
            return None
        return {**value, "messages": [dict(item) for item in self.messages if item["conversation_id"] == conversation_id]}

    def rename_qa_conversation(self, *, user_id: str, conversation_id: str, title: str) -> bool:
        value = self.conversations.get(conversation_id)
        if not value or value["user_id"] != user_id:
            return False
        value["title"] = title[:300]
        return True

    def delete_qa_conversation(self, *, user_id: str, conversation_id: str) -> bool:
        value = self.conversations.get(conversation_id)
        if not value or value["user_id"] != user_id:
            return False
        value["status"] = "deleted"
        return True
