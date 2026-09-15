from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import settings


class DatabaseUnavailable(RuntimeError):
    pass


class PostgresRagRepository:
    """Minimal repository for tables owned by the RAG service.

    psycopg is imported lazily so document fixtures and API health checks work
    without a database driver. SQL never interpolates user values; only the
    validated configured schema is interpolated for table qualification.
    """

    def __init__(self, *, connection_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._pool: Any | None = None
        self.schema = _safe_schema(settings.database_schema)

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
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise DatabaseUnavailable("psycopg_pool is required for RAG persistence") from exc
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
                    pooled.rollback()
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

    def create_processing_job(self, envelope: dict[str, Any], *, job_type: str) -> str | None:
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        event_id = str(envelope.get("event_id") or "")
        if not event_id:
            raise ValueError("event_id is required")
        knowledge_item_id = str(payload.get("knowledge_item_id") or "")
        if not knowledge_item_id:
            raise ValueError("payload.knowledge_item_id is required")
        payload_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.processing_jobs
                    (source_event_id,event_type,payload_hash,organization_id,knowledge_item_id,content_version,acl_version,job_type)
                    VALUES (%s::uuid,%s,%s,%s::uuid,%s::uuid,%s,%s,%s)
                    -- Keep the first payload immutable. A repeated event_id
                    -- with a different payload must be rejected below rather
                    -- than silently replacing the idempotency fingerprint.
                    ON CONFLICT (source_event_id) DO UPDATE
                      SET payload_hash={self.schema}.processing_jobs.payload_hash
                    RETURNING id::text,payload_hash""",
                    (event_id, str(envelope.get("event_type") or ""), payload_hash, envelope.get("organization_id"), knowledge_item_id, int(payload.get("content_version") or 1), int(payload.get("acl_version") or 0), job_type),
                )
                row = cursor.fetchone()
        if row and str(row[1]) != payload_hash:
            raise ValueError("source event payload changed for an existing event_id")
        return str(row[0]) if row else None

    def get_processing_job(self, source_event_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT id::text,status FROM {self.schema}.processing_jobs WHERE source_event_id=%s::uuid", (source_event_id,))
                row = cursor.fetchone()
        return {"id": str(row[0]), "status": str(row[1])} if row else None

    def update_processing_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "current_stage", "parser_name", "parser_version", "parsed_artifact_ref", "parsed_content_hash", "page_count", "chunking_version", "embedding_model", "retry_count", "started_at", "finished_at", "last_error"}
        values = [(key, fields[key]) for key in fields if key in allowed]
        if not values:
            return
        assignments = ", ".join(f"{key}=%s" for key, _ in values)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {self.schema}.processing_jobs SET {assignments} WHERE id=%s::uuid", [value for _, value in values] + [job_id])

    def upsert_index_record(self, *, knowledge_item_id: str, organization_id: str | None, content_version: int, acl_version: int, content_variant: str, es_index_alias: str, es_document_prefix: str, chunk_count: int, mapping_version: str, status: str) -> None:
        if content_variant not in {"display", "protected"}:
            raise ValueError("unsupported content variant")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.index_records
                    (knowledge_item_id,organization_id,content_version,acl_version,content_variant,es_index_alias,es_document_prefix,chunk_count,mapping_version,status,indexed_at,updated_at)
                    VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='ready' THEN CURRENT_TIMESTAMP ELSE NULL END,CURRENT_TIMESTAMP)
                    ON CONFLICT (knowledge_item_id,content_version,content_variant) DO UPDATE SET
                      acl_version=EXCLUDED.acl_version,es_index_alias=EXCLUDED.es_index_alias,
                      es_document_prefix=EXCLUDED.es_document_prefix,chunk_count=EXCLUDED.chunk_count,
                      mapping_version=EXCLUDED.mapping_version,status=EXCLUDED.status,
                      indexed_at=EXCLUDED.indexed_at,updated_at=CURRENT_TIMESTAMP""",
                    (knowledge_item_id, organization_id, content_version, acl_version, content_variant, es_index_alias, es_document_prefix, chunk_count, mapping_version, status, status),
                )

    def record_search(self, *, user_id: str, organization_id: str | None, query_text: str, query_hash: str, filters: dict[str, Any], result_count: int, duration_ms: int, request_id: str | None) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.search_history
                    (user_id,organization_id,query_text,query_hash,filters,result_count,duration_ms,request_id)
                    VALUES (%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s,%s,%s)""",
                    (user_id, organization_id, query_text, query_hash, json.dumps(filters, ensure_ascii=False), result_count, duration_ms, request_id),
                )

    def add_outbox_event(self, envelope: dict[str, Any], *, aggregate_type: str, aggregate_id: str, event_version: int = 1) -> str:
        event_id = str(envelope.get("event_id") or uuid.uuid4())
        event_type = str(envelope.get("event_type") or "")
        if event_type not in {"document.extracted", "processing.completed", "processing.failed"}:
            raise ValueError("unsupported RAG outbox event")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.outbox_events
                    (id,aggregate_type,aggregate_id,event_type,event_version,schema_version,organization_id,trace_id,payload)
                    VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s,%s::uuid,%s,%s::jsonb)
                    ON CONFLICT DO NOTHING""",
                    (event_id, aggregate_type, aggregate_id, event_type, event_version, int(envelope.get("schema_version") or 1), envelope.get("organization_id"), str(envelope.get("trace_id") or ""), json.dumps(envelope.get("payload") or {}, ensure_ascii=False)),
                )
        return event_id

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""SELECT id::text,event_type,schema_version,organization_id,trace_id,payload
                    FROM {self.schema}.outbox_events
                    WHERE status IN ('pending','failed') AND available_at <= CURRENT_TIMESTAMP
                    ORDER BY created_at LIMIT %s""", (max(1, limit),))
                rows = cursor.fetchall()
        return [{"event_id": row[0], "event_type": row[1], "schema_version": row[2], "organization_id": str(row[3]) if row[3] else None, "trace_id": row[4], "producer": settings.service_name, "payload": row[5] or {}} for row in rows]

    def mark_outbox_published(self, event_id: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {self.schema}.outbox_events SET status='published',published_at=CURRENT_TIMESTAMP WHERE id=%s::uuid", (event_id,))

    def create_qa_conversation(self, *, user_id: str, organization_id: str | None, title: str | None = None) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {self.schema}.qa_conversations (user_id,organization_id,title) VALUES (%s::uuid,%s::uuid,%s) RETURNING id::text", (user_id, organization_id, title))
                row = cursor.fetchone()
        return str(row[0])

    def add_qa_message(self, *, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]] | None = None, model_name: str | None = None, prompt_version: str | None = None, status: str = "completed") -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {self.schema}.qa_messages (conversation_id,role,content,citations,model_name,prompt_version,status) VALUES (%s::uuid,%s,%s,%s::jsonb,%s,%s,%s) RETURNING id::text", (conversation_id, role, content, json.dumps(citations or [], ensure_ascii=False), model_name, prompt_version, status))
                row = cursor.fetchone()
        return str(row[0])


class InMemoryRagRepository:
    """Deterministic repository used by unit tests and local fixtures."""

    def __init__(self) -> None:
        self.processing_jobs: dict[str, dict[str, Any]] = {}
        self.index_records: list[dict[str, Any]] = []
        self.search_history: list[dict[str, Any]] = []
        self.outbox_events: list[dict[str, Any]] = []
        self.qa_conversations: list[dict[str, Any]] = []
        self.qa_messages: list[dict[str, Any]] = []

    def create_processing_job(self, envelope: dict[str, Any], *, job_type: str) -> str | None:
        event_id = str(envelope.get("event_id") or "")
        if not event_id:
            raise ValueError("event_id is required")
        existing = self.processing_jobs.get(event_id)
        if existing:
            payload_hash = hashlib.sha256(json.dumps(envelope.get("payload") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if existing.get("payload_hash") != payload_hash:
                raise ValueError("source event payload changed for an existing event_id")
            return existing["id"]
        job_id = str(uuid.uuid4())
        payload = envelope.get("payload") or {}
        payload_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.processing_jobs[event_id] = {"id": job_id, "event_type": envelope.get("event_type"), "job_type": job_type, "status": "pending", "knowledge_item_id": payload.get("knowledge_item_id"), "content_version": payload.get("content_version", 1), "acl_version": payload.get("acl_version", 0), "payload_hash": payload_hash}
        return job_id

    def get_processing_job(self, source_event_id: str) -> dict[str, Any] | None:
        return self.processing_jobs.get(source_event_id)

    def update_processing_job(self, job_id: str, **fields: Any) -> None:
        for value in self.processing_jobs.values():
            if value["id"] == job_id:
                value.update(fields)
                return

    def upsert_index_record(self, **value: Any) -> None:
        for item in self.index_records:
            if (item.get("knowledge_item_id"), item.get("content_version"), item.get("content_variant")) == (value.get("knowledge_item_id"), value.get("content_version"), value.get("content_variant")):
                item.update(value)
                return
        self.index_records.append(dict(value))

    def delete_older_versions(self, *, knowledge_item_id: str, content_version: int) -> int:
        removed = 0
        for item in self.index_records:
            if item.get("knowledge_item_id") == knowledge_item_id and int(item.get("content_version", 0)) < content_version:
                item["status"] = "deleted"
                removed += 1
        return removed

    def record_search(self, **value: Any) -> None:
        self.search_history.append(dict(value))

    def add_outbox_event(self, envelope: dict[str, Any], **_: Any) -> str:
        event_id = str(envelope.get("event_id") or uuid.uuid4())
        self.outbox_events.append({"id": event_id, "status": "pending", **envelope})
        return event_id

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [{key: value for key, value in item.items() if key != "id" and key != "status"} for item in self.outbox_events if item.get("status") in {"pending", "failed"}][: max(1, limit)]

    def mark_outbox_published(self, event_id: str) -> None:
        for item in self.outbox_events:
            if item.get("event_id") == event_id or item.get("id") == event_id:
                item["status"] = "published"

    def create_qa_conversation(self, *, user_id: str, organization_id: str | None, title: str | None = None) -> str:
        value = str(uuid.uuid4())
        self.qa_conversations.append({"id": value, "user_id": user_id, "organization_id": organization_id, "title": title})
        return value

    def add_qa_message(self, *, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]] | None = None, model_name: str | None = None, prompt_version: str | None = None, status: str = "completed") -> str:
        value = str(uuid.uuid4())
        self.qa_messages.append({"id": value, "conversation_id": conversation_id, "role": role, "content": content, "citations": citations or [], "model_name": model_name, "prompt_version": prompt_version, "status": status})
        return value


def _safe_schema(value: str) -> str:
    value = (value or "rag").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("RAG_DATABASE_SCHEMA must be a simple SQL identifier")
    return value
