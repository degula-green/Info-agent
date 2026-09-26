from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app.config import settings
from app.domain.rag import (
    BranchMatch,
    Candidate,
    Chunk,
    Entity,
    EntityAlias,
    ResourceContext,
    stable_id,
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


def _as_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _looks_like_month(value: str) -> bool:
    return bool(re.search(r":\d{4}-\d{2}$", value or ""))


class PostgresRagMVPRepository:
    def __init__(self, *, connection_factory: Any | None = None) -> None:
        self.schema = _safe_schema(settings.database_schema)
        self._connection_factory = connection_factory
        self._pool: Any | None = None

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

    def create_or_get_job(self, envelope: dict[str, Any], *, processing_version: str | None = None) -> dict[str, Any]:
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
        scope_type = "organization" if audience in {"organization_members", "source_conversation_members"} else "user"
        scope_id = str(envelope.get("organization_id") or ZERO_UUID)
        knowledge_base_id = str(payload.get("knowledge_base_id") or ZERO_UUID)
        version = processing_version or settings.processing_version
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.processing_jobs
                    (source_event_id,payload_hash,job_type,knowledge_item_id,resource_type,resource_id,
                     knowledge_base_id,scope_type,scope_id,source_conversation_id,source_audience_policy,
                     content_version,processing_version,acl_version)
                    VALUES (%s::uuid,%s,'full_process',%s::uuid,%s,%s::uuid,%s::uuid,%s,%s::uuid,
                            %s::uuid,%s,%s,%s,%s)
                    ON CONFLICT (source_event_id) DO UPDATE SET payload_hash={self.schema}.processing_jobs.payload_hash
                    RETURNING id::text,payload_hash,status,current_stage""",
                    (
                        source_event_id, hash_value, knowledge_item_id, resource_type, resource_id,
                        knowledge_base_id, scope_type, scope_id,
                        _uuid_or_none(payload.get("source_conversation_id")), audience or None,
                        int(payload.get("content_version") or 1), version,
                        int(payload.get("acl_version") or 0),
                    ),
                )
                row = cursor.fetchone()
                if row and str(row[1]) != hash_value:
                    raise ValueError("source_event_id payload changed")
                job_id = str(row[0])
                cursor.execute(
                    f"""SELECT id::text,status,current_stage,lease_owner,lease_until,retry_count,next_retry_at
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
                    f"""SELECT id::text,status,current_stage,lease_owner,lease_until,retry_count,next_retry_at,
                               source_event_id::text,knowledge_item_id::text,resource_type,resource_id::text,
                               knowledge_base_id::text,scope_type,scope_id::text,source_conversation_id::text,
                               source_audience_policy,content_version,processing_version,acl_version,last_error
                        FROM {self.schema}.processing_jobs WHERE {where}""",
                    (value,),
                )
                row = cursor.fetchone()
                return self._full_job_row(row) if row else None

    def claim_jobs(self, lane: str, *, limit: int = 1, lease_seconds: int | None = None) -> list[dict[str, Any]]:
        if lane not in {"parse", "index", "memory"}:
            raise ValueError("lane must be parse, index, or memory")
        lease = int(lease_seconds or settings.task_lease_seconds)
        stage_clause = {
            "parse": "(status='pending' OR current_stage IN ('fetch','parse','chunk'))",
            "index": "(status='processing' AND current_stage='index')",
            "memory": "(status='ready' AND current_stage='memory')",
        }[lane]
        owner = f"{settings.service_name}:{lane}:{uuid.uuid4().hex[:12]}"
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""WITH candidates AS (
                          SELECT id FROM {self.schema}.processing_jobs
                          WHERE {stage_clause}
                            AND next_retry_at<=CURRENT_TIMESTAMP
                            AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
                          ORDER BY created_at
                          FOR UPDATE SKIP LOCKED
                          LIMIT %s
                        )
                        UPDATE {self.schema}.processing_jobs j
                        SET lease_owner=%s,lease_until=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                            status=CASE WHEN j.status='pending' THEN 'processing' ELSE j.status END,
                            started_at=COALESCE(j.started_at,CURRENT_TIMESTAMP),
                            updated_at=CURRENT_TIMESTAMP
                        FROM candidates c WHERE j.id=c.id
                        RETURNING j.id::text,j.status,j.current_stage,j.lease_owner,j.lease_until,
                                  j.retry_count,j.next_retry_at,j.source_event_id::text,
                                  j.knowledge_item_id::text,j.resource_type,j.resource_id::text,
                                  j.knowledge_base_id::text,j.scope_type,j.scope_id::text,
                                  j.source_conversation_id::text,j.source_audience_policy,
                                  j.content_version,j.processing_version,j.acl_version,j.last_error""",
                    (limit, owner, lease),
                )
                return [self._full_job_row(row) for row in cursor.fetchall()]

    def heartbeat(self, job_id: str, *, lease_seconds: int | None = None) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.processing_jobs
                        SET lease_until=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s::uuid""",
                    (int(lease_seconds or settings.task_lease_seconds), job_id),
                )

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "knowledge_base_id", "scope_type", "scope_id", "source_conversation_id",
            "source_audience_policy", "status", "current_stage", "lease_owner", "lease_until",
            "retry_count", "next_retry_at", "last_error", "started_at", "finished_at",
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
                cursor.execute(
                    f"UPDATE {self.schema}.processing_jobs SET {', '.join(assignments)} WHERE id=%s::uuid",
                    (*params, job_id),
                )

    def add_attempt(
        self,
        job_id: str,
        *,
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
                cursor.execute(
                    f"""SELECT COALESCE(MAX(attempt),0)+1 FROM {self.schema}.processing_job_attempts
                        WHERE job_id=%s::uuid AND stage=%s""",
                    (job_id, stage),
                )
                attempt = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""INSERT INTO {self.schema}.processing_job_attempts
                    (job_id,lane,stage,attempt,status,retryable,error_code,error_message,metrics,finished_at)
                    VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                            CASE WHEN %s='running' THEN NULL ELSE CURRENT_TIMESTAMP END)
                    RETURNING id::text""",
                    (
                        job_id, lane, stage, attempt, status, retryable, error_code,
                        (error_message or "")[:2000] or None, _as_json(metrics), status,
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
                    f"""SELECT chunk_id,resource_snapshot_id::text,knowledge_item_id::text,resource_type,
                               resource_id::text,knowledge_base_id::text,scope_type,scope_id::text,
                               content_version,processing_version,chunking_version,content_variant,
                               chunk_index,chunk_count,title,file_name,heading_path,context_header,content,
                               content_hash,source_locator,source_conversation_id::text,conversation_type,
                               document_id::text,message_id::text,sent_at,auth_partition_key,auth_object_key,
               acl_version,sensitivity,embedding_model,embedding_dimensions,
                               embedding_status,rag_eligible,lifecycle_status
                        FROM {self.schema}.chunks WHERE {where} ORDER BY chunk_index""",
                    tuple(params),
                )
                return [self._chunk_row(row) for row in cursor.fetchall()]

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
                    ON CONFLICT (chunk_id,chunk_variant,mapping_version) DO UPDATE SET
                      es_index_alias=EXCLUDED.es_index_alias,embedding_model=EXCLUDED.embedding_model,
                      status=EXCLUDED.status,indexed_at=EXCLUDED.indexed_at,last_error=NULL,
                      updated_at=CURRENT_TIMESTAMP""",
                    (
                        chunk.chunk_id, chunk.content_variant, chunk.scope_type, chunk.scope_id,
                        chunk.knowledge_base_id, alias, chunk.chunk_id, mapping_version,
                        chunk.embedding_model, status, status,
                    ),
                )

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

    def ensure_tree_branch(
        self,
        *,
        scope_type: str,
        scope_id: str,
        domain: str,
        entity_id: str,
        entity_name: str,
        bucket: str | None,
        registry_version: int,
        branch_key: str | None = None,
    ) -> str:
        branch_key = branch_key or (
            f"entity:{domain}:{entity_id}" + (f":{bucket}" if bucket else "")
        )
        scope_node_key = f"scope:{scope_type}:{scope_id}"
        domain_node_key = f"domain:{domain}"
        entity_node_key = f"entity:{domain}:{entity_id}"
        with self._connection() as connection:
            with connection.cursor() as cursor:
                scope_id_value = self._ensure_tree_node(
                    cursor, scope_type, scope_id, domain, None, None, "scope", scope_node_key,
                    f"scope:{scope_type}:{scope_id}", registry_version,
                )
                domain_id = self._ensure_tree_node(
                    cursor, scope_type, scope_id, domain, None, scope_id_value, "domain",
                    domain_node_key, f"domain:{domain}", registry_version,
                )
                entity_id_value = self._ensure_tree_node(
                    cursor, scope_type, scope_id, domain, entity_id, domain_id, "entity",
                    entity_node_key, f"entity:{domain}:{entity_id}", registry_version,
                )
                if bucket:
                    self._ensure_tree_node(
                        cursor, scope_type, scope_id, domain, entity_id, entity_id_value, "time",
                        f"{entity_node_key}:{bucket}", branch_key, registry_version,
                    )
                elif not bucket:
                    cursor.execute(
                        f"""UPDATE {self.schema}.tree_nodes SET branch_key=%s,updated_at=CURRENT_TIMESTAMP
                            WHERE id=%s::uuid""",
                        (branch_key, entity_id_value),
                    )
        return branch_key

    def replace_chunk_branches(self, chunk: Chunk, branches: list[BranchMatch]) -> None:
        for branch in branches:
            self.ensure_tree_branch(
                scope_type=chunk.scope_type,
                scope_id=chunk.scope_id,
                domain=branch.domain,
                entity_id=branch.entity_id,
                entity_name=branch.entity_id,
                bucket=branch.branch_key.rsplit(":", 1)[-1] if _looks_like_month(branch.branch_key) else None,
                registry_version=branch.registry_version,
                branch_key=branch.branch_key,
            )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                keys = [branch.branch_key for branch in branches]
                if keys:
                    cursor.execute(
                        f"""UPDATE {self.schema}.chunk_branches SET status='removed',updated_at=CURRENT_TIMESTAMP
                            WHERE chunk_id=%s AND status='active' AND NOT (branch_key=ANY(%s))""",
                        (chunk.chunk_id, keys),
                    )
                else:
                    cursor.execute(
                        f"""UPDATE {self.schema}.chunk_branches SET status='removed',updated_at=CURRENT_TIMESTAMP
                            WHERE chunk_id=%s AND status='active'""",
                        (chunk.chunk_id,),
                    )
                for branch in branches:
                    cursor.execute(
                        f"""INSERT INTO {self.schema}.chunk_branches
                        (chunk_id,branch_key,entity_id,scope_type,scope_id,registry_version,
                         match_method,match_score,status)
                        VALUES (%s,%s,%s::uuid,%s,%s::uuid,%s,%s,%s,'active')
                        ON CONFLICT (chunk_id,branch_key) DO UPDATE SET
                          entity_id=EXCLUDED.entity_id,registry_version=EXCLUDED.registry_version,
                          match_method=EXCLUDED.match_method,match_score=EXCLUDED.match_score,
                          status='active',updated_at=CURRENT_TIMESTAMP""",
                        (
                            chunk.chunk_id, branch.branch_key, branch.entity_id, chunk.scope_type,
                            chunk.scope_id, branch.registry_version, branch.match_method,
                            branch.match_score,
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
                    VALUES (%s,%s::uuid,%s,%s,%s,1,1,1,CASE WHEN %s::text='' THEN 0 ELSE 1 END,%s,0.5,'new')
                    ON CONFLICT (scope_type,scope_id,candidate_domain,normalized_key) DO UPDATE SET
                      candidate_name=EXCLUDED.candidate_name,mention_count=entity_candidates.mention_count+1,
                      sample_context=COALESCE(entity_candidates.sample_context,EXCLUDED.sample_context),
                      last_seen_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    RETURNING id::text""",
                    (
                        scope_type, scope_id, candidate_name, normalized_key, domain,
                        chunk.source_conversation_id or "", context_excerpt[:500],
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
                        f"SELECT registry_version FROM {self.schema}.entity_registry WHERE id=%s::uuid",
                        (target_entity_id,),
                    )
                    registry_version = int((cursor.fetchone() or [1])[0])
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
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""SELECT COALESCE(MAX(registry_version),1) FROM {self.schema}.tree_nodes
                        WHERE scope_type=%s AND scope_id=%s::uuid""",
                    (scope_type, scope_id),
                )
                version = int(cursor.fetchone()[0] or 1)
                cursor.execute(
                    f"""SELECT id::text,node_type,domain,entity_id::text,time_bucket,node_key,
                               branch_key,parent_id::text,registry_version,statistics
                        FROM {self.schema}.tree_nodes
                        WHERE scope_type=%s AND scope_id=%s::uuid AND status='active'
                        ORDER BY node_type,node_key""",
                    (scope_type, scope_id),
                )
                nodes = []
                for row in cursor.fetchall():
                    nodes.append({
                        "node_id": row[0], "node_type": row[1], "domain": row[2],
                        "entity_id": row[3], "time_bucket": row[4], "node_key": row[5],
                        "branch_key": row[6], "parent_id": row[7],
                        "registry_version": int(row[8]), "statistics": row[9] or {},
                    })
                return {"scope_key": f"{scope_type}:{scope_id}", "registry_version": version, "nodes": nodes}

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
                    f"""SELECT id::text,title,retrieval_mode,knowledge_base_ids,created_at,updated_at
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

    def _ensure_tree_node(
        self,
        cursor: Any,
        scope_type: str,
        scope_id: str,
        domain: str,
        entity_id: str | None,
        parent_id: str | None,
        node_type: str,
        node_key: str,
        branch_key: str,
        registry_version: int,
    ) -> str:
        cursor.execute(
            f"""INSERT INTO {self.schema}.tree_nodes
            (scope_type,scope_id,domain,entity_id,parent_id,node_type,node_key,branch_key,registry_version)
            VALUES (%s,%s::uuid,%s,%s::uuid,%s::uuid,%s,%s,%s,%s)
            ON CONFLICT (scope_type,scope_id,node_key) DO UPDATE SET
              parent_id=COALESCE(EXCLUDED.parent_id,{self.schema}.tree_nodes.parent_id),
              branch_key=EXCLUDED.branch_key,registry_version=EXCLUDED.registry_version,
              status='active',updated_at=CURRENT_TIMESTAMP
            RETURNING id::text""",
            (
                scope_type, scope_id, domain, entity_id, parent_id, node_type, node_key,
                branch_key, registry_version,
            ),
        )
        return str(cursor.fetchone()[0])

    @staticmethod
    def _job_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]), "status": row[1], "current_stage": row[2],
            "lease_owner": row[3], "lease_until": row[4], "retry_count": int(row[5] or 0),
            "next_retry_at": row[6],
        }

    @staticmethod
    def _full_job_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]), "status": row[1], "current_stage": row[2],
            "lease_owner": row[3], "lease_until": row[4], "retry_count": int(row[5] or 0),
            "next_retry_at": row[6], "source_event_id": str(row[7]),
            "knowledge_item_id": str(row[8]), "resource_type": row[9], "resource_id": str(row[10]),
            "knowledge_base_id": str(row[11]), "scope_type": row[12], "scope_id": str(row[13]),
            "source_conversation_id": str(row[14]) if row[14] else None,
            "source_audience_policy": row[15], "content_version": int(row[16]),
            "processing_version": row[17], "acl_version": int(row[18] or 0), "last_error": row[19],
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
        self.tree_nodes: dict[str, dict[str, Any]] = {}
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
                and item["status"] in {"pending", "processing", "ready", "metadata_only"}
            ):
                self.events[event_id] = digest
                return dict(item)
        audience = str(payload.get("source_audience_policy") or "")
        scope_type = "organization" if audience in {"organization_members", "source_conversation_members"} else "user"
        job_id = new_uuid()
        value = {
            "id": job_id, "source_event_id": event_id, "payload_hash": digest,
            "job_type": "full_process", "knowledge_item_id": resource_key[0],
            "resource_type": resource_key[1], "resource_id": resource_key[2],
            "knowledge_base_id": str(payload.get("knowledge_base_id") or ZERO_UUID),
            "scope_type": scope_type, "scope_id": str(envelope.get("organization_id") or ZERO_UUID),
            "source_conversation_id": payload.get("source_conversation_id"),
            "source_audience_policy": audience or None, "content_version": resource_key[3],
            "processing_version": processing_version or settings.processing_version,
            "acl_version": int(payload.get("acl_version") or 0), "status": "pending",
            "current_stage": None, "lease_owner": None, "lease_until": None,
            "retry_count": 0, "next_retry_at": datetime.now(timezone.utc), "last_error": None,
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

    def claim_jobs(self, lane: str, *, limit: int = 1, lease_seconds: int | None = None) -> list[dict[str, Any]]:
        def eligible(item: dict[str, Any]) -> bool:
            if item["status"] == "pending" or item.get("current_stage") in {"fetch", "parse", "chunk"}:
                return lane == "parse"
            if item["status"] == "processing" and item.get("current_stage") == "index":
                return lane == "index"
            if item["status"] == "ready" and item.get("current_stage") == "memory":
                return lane == "memory"
            return False

        output = []
        for item in self.jobs.values():
            if len(output) >= limit:
                break
            if not eligible(item):
                continue
            if item.get("lease_until") and item["lease_until"] > datetime.now(timezone.utc):
                continue
            if item["status"] == "pending":
                item["status"] = "processing"
            item["lease_owner"] = f"{settings.service_name}:{lane}"
            item["lease_until"] = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds or settings.task_lease_seconds)
            output.append(dict(item))
        return output

    def heartbeat(self, job_id: str, *, lease_seconds: int | None = None) -> None:
        self.jobs[job_id]["lease_until"] = datetime.now(timezone.utc) + timedelta(
            seconds=lease_seconds or settings.task_lease_seconds
        )

    def update_job(self, job_id: str, **fields: Any) -> None:
        self.jobs[job_id].update(fields)

    def add_attempt(self, job_id: str, **fields: Any) -> str:
        value = {"id": new_uuid(), "job_id": job_id, **fields}
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
        return [item for item in sorted(values, key=lambda value: value.chunk_index)]

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

    def upsert_projection(self, chunk: Chunk, **value: Any) -> None:
        self.projections.append({"chunk_id": chunk.chunk_id, **value})

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

    def upsert_entity(
        self,
        *,
        entity_id: str,
        scope_type: str,
        scope_id: str,
        domain: str,
        canonical_name: str,
        normalized_key: str,
        registry_version: int = 1,
    ) -> None:
        value = {
            "id": entity_id, "scope_type": scope_type, "scope_id": scope_id, "domain": domain,
            "canonical_name": canonical_name, "normalized_key": normalized_key, "status": "active",
            "registry_version": registry_version,
        }
        self.entities = [item for item in self.entities if item["id"] != entity_id] + [value]

    def upsert_alias(self, **value: Any) -> None:
        self.aliases = [item for item in self.aliases if item["id"] != value["id"]] + [value]

    def ensure_tree_branch(self, **value: Any) -> str:
        branch_key = str(value["branch_key"])
        self.tree_nodes[branch_key] = {
            "node_id": value.get("node_id") or new_uuid(),
            "scope_key": f"{value['scope_type']}:{value['scope_id']}",
            "domain": value["domain"],
            "entity_id": value["entity_id"],
            "time_bucket": value.get("bucket"),
            "branch_key": branch_key,
            "node_type": "time" if value.get("bucket") else "entity",
            "registry_version": value["registry_version"],
            "statistics": {},
            "status": "active",
        }
        return branch_key

    def replace_chunk_branches(self, chunk: Chunk, branches: list[BranchMatch]) -> None:
        for key in list(self.branches):
            if key[0] == chunk.chunk_id:
                self.branches.pop(key)
        for branch in branches:
            self.ensure_tree_branch(
                scope_type=chunk.scope_type,
                scope_id=chunk.scope_id,
                domain=branch.domain,
                entity_id=branch.entity_id,
                entity_name=branch.entity_id,
                bucket=(
                    branch.branch_key.rsplit(":", 1)[-1]
                    if re.search(r":\d{4}-\d{2}$", branch.branch_key) else None
                ),
                registry_version=branch.registry_version,
                branch_key=branch.branch_key,
            )
            self.branches[(chunk.chunk_id, branch.branch_key)] = {
                **branch.__dict__, "status": "active",
            }

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
                "sample_context": context_excerpt, "score": 0.5, "status": "new",
                "suggested_entity_id": None, "resolved_entity_id": None,
            }
        value = self.candidates[candidate_id]
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
        if value.get("expected_status") and candidate["status"] != value["expected_status"]:
            raise ValueError("candidate status changed")
        key = (value["candidate_id"], value["review_request_id"])
        existing = next((item for item in self.reviews if (item["candidate_id"], item["review_request_id"]) == key), None)
        if existing:
            return {**existing, "idempotent": True}
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
            version = next((item["registry_version"] for item in self.entities if item["id"] == resolved), 1)
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
        nodes = [dict(value) for value in self.tree_nodes.values() if value["scope_key"] == prefix]
        return {
            "scope_key": prefix,
            "registry_version": max((int(value.get("registry_version") or 1) for value in nodes), default=1),
            "nodes": nodes,
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
