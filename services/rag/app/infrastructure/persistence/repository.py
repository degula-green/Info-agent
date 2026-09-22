from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import settings
from app.domain.memory import FactCandidate, MemoryGraph, SourceRouteCandidate, normalize_key
from app.domain.models import AttachmentContext, ChunkRecord


_MEMORY_NAMESPACE = uuid.UUID("7f909ab4-5e80-4bcb-9372-814c2466dc9f")


def _stable_uuid(*parts: Any) -> str:
    return str(uuid.uuid5(_MEMORY_NAMESPACE, "|".join(str(part or "") for part in parts)))


def _uuid(value: str | None, *fallback: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return _stable_uuid(*(fallback or (value,)))


def _memory_plan(context: AttachmentContext, chunks: list[ChunkRecord], facts: list[FactCandidate], routes: list[SourceRouteCandidate] | None = None, processing_status: str = "ready", processing_error: dict[str, Any] | None = None) -> dict[str, Any]:
    knowledge_item_id = _uuid(context.knowledge_item_id or context.attachment_id, "item", context.knowledge_item_id or context.attachment_id)
    knowledge_base_id = _uuid(context.knowledge_base_id, "kb", context.knowledge_base_id or knowledge_item_id)
    organization_id = _uuid(context.organization_id, "org", context.organization_id) if context.organization_id else None
    owner_user_id = _uuid(context.owner_user_id, "user", context.owner_user_id) if context.owner_user_id and not organization_id else None
    source_type = "message" if context.message_id else ("attachment" if context.attachment_id else "knowledge_item")
    source_resource_id = context.message_id or context.attachment_id or knowledge_item_id
    source_id = _stable_uuid("source", source_type, source_resource_id, context.content_version, settings.memory_extraction_version)
    source_hash = (context.source_content_hash or hashlib.sha256("\n".join(chunk.content for chunk in chunks).encode()).hexdigest()).removeprefix("sha256:")
    visibility = "protected" if any(chunk.protected for chunk in chunks) else "display"
    auth_object_key = next((chunk.auth_object_key for chunk in chunks if chunk.auth_object_key), None)
    chunk_rows, external_to_id = [], {}
    for chunk in chunks:
        chunk_id = _stable_uuid("chunk", chunk.chunk_id)
        external_to_id[chunk.chunk_id] = chunk_id
        chunk_rows.append({"id": chunk_id, "external_chunk_id": chunk.chunk_id, "source_id": source_id, "chunk": chunk})
    # Extractors may mention the same fact in overlapping chunks. Collapse it
    # before building projections while retaining every evidence chunk.
    unique_facts: dict[str, FactCandidate] = {}
    for candidate in facts:
        current = unique_facts.get(candidate.dedupe_key)
        if current is None:
            unique_facts[candidate.dedupe_key] = candidate
        else:
            unique_facts[candidate.dedupe_key] = replace(
                current,
                chunk_ids=tuple(dict.fromkeys((*current.chunk_ids, *candidate.chunk_ids))),
            )
    entity_rows: dict[tuple[str, str], dict[str, Any]] = {}
    fact_rows: list[dict[str, Any]] = []
    for fact in unique_facts.values():
        entity_key = normalize_key(fact.subject)
        entity_id = _stable_uuid("entity", knowledge_base_id, fact.entity_type, entity_key)
        entity_rows[(fact.entity_type, entity_key)] = {"id": entity_id, "fact": fact, "normalized_key": entity_key}
        fact_id = _stable_uuid("fact", knowledge_base_id, fact.dedupe_key)
        version_id = _stable_uuid("fact-version", fact_id, context.content_version, hashlib.sha256(fact.text.encode()).hexdigest())
        fact_rows.append({"id": fact_id, "version_id": version_id, "entity_id": entity_id, "fact": fact, "chunk_ids": [external_to_id[value] for value in fact.chunk_ids if value in external_to_id]})
    for route in routes or ():
        entity_key = normalize_key(route.subject)
        entity_rows.setdefault((route.entity_type, entity_key), {
            "id": _stable_uuid("entity", knowledge_base_id, route.entity_type, entity_key),
            "route": route,
            "normalized_key": entity_key,
        })
    session_subject = context.conversation_group_id or context.external_conversation_id or context.conversation_ingestion_id or context.message_id or knowledge_item_id
    month = (context.sent_at or "")[:7] if len(context.sent_at or "") >= 7 else "general"
    trees: dict[tuple[str, str], dict[str, Any]] = {}
    mounts: list[dict[str, Any]] = []
    source_mounts: list[dict[str, Any]] = []

    def ensure_path(tree_type: str, subject: str, group: str, topic: str) -> tuple[dict[str, Any], str]:
        subject_key, group_key, topic_key = normalize_key(subject), normalize_key(group), normalize_key(topic)
        tree_id = _stable_uuid("tree", knowledge_base_id, tree_type, subject_key)
        root_id = _stable_uuid("node", tree_id, "root")
        internal_id = _stable_uuid("node", tree_id, "group", group_key)
        leaf_id = _stable_uuid("node", tree_id, "leaf", group_key, topic_key)
        tree = trees.setdefault((tree_type, subject_key), {"id": tree_id, "tree_type": tree_type, "subject_key": subject_key, "root_id": root_id, "nodes": {}})
        tree["nodes"][root_id] = {"id": root_id, "parent_id": None, "node_type": "root", "level": 0, "node_key": "root", "title": subject_key, "topic_key": None, "phase_key": None, "time_start": context.sent_at, "time_end": context.sent_at}
        tree["nodes"][internal_id] = {"id": internal_id, "parent_id": root_id, "node_type": "internal", "level": 1, "node_key": f"group:{group_key}", "title": group_key, "topic_key": None, "phase_key": group_key if tree_type == "entity" else None, "time_start": context.sent_at, "time_end": context.sent_at}
        tree["nodes"][leaf_id] = {"id": leaf_id, "parent_id": internal_id, "node_type": "leaf", "level": 2, "node_key": f"leaf:{group_key}:{topic_key}", "title": topic_key, "topic_key": topic_key, "phase_key": group_key if tree_type == "entity" else None, "time_start": context.sent_at, "time_end": context.sent_at}
        return tree, leaf_id

    for fact_row in fact_rows:
        fact = fact_row["fact"]
        session_tree, session_leaf = ensure_path("session", session_subject, month, fact.topic or "general")
        entity_tree, entity_leaf = ensure_path("entity", fact.subject, fact.phase or "general", fact.topic or "general")
        mounts.extend((
            {"tree": session_tree, "leaf_id": session_leaf, "fact": fact_row},
            {"tree": entity_tree, "leaf_id": entity_leaf, "fact": fact_row},
        ))
    for route in routes or ():
        entity_tree, entity_leaf = ensure_path("entity", route.subject, route.phase or "general", route.topic or "general")
        session_tree, session_leaf = ensure_path("session", session_subject, month, route.topic or "general")
        source_mounts.extend((
            {"tree": entity_tree, "leaf_id": entity_leaf, "route": route},
            {"tree": session_tree, "leaf_id": session_leaf, "route": route},
        ))
    return {"knowledge_item_id": knowledge_item_id, "knowledge_base_id": knowledge_base_id, "organization_id": organization_id, "owner_user_id": owner_user_id, "source_id": source_id, "source_type": source_type, "source_resource_id": source_resource_id, "source_hash": source_hash, "visibility": visibility, "auth_object_key": auth_object_key, "content_version": context.content_version, "acl_version": context.acl_version, "access_scope": context.access_scope, "sensitivity": context.sensitivity, "attachment_id": _uuid(context.attachment_id, "attachment", context.attachment_id) if context.attachment_id else None, "conversation_group_id": context.conversation_group_id or context.external_conversation_id, "message_id": context.message_id, "observed_at": context.sent_at or None, "processing_status": processing_status, "processing_error": processing_error or {}, "chunks": chunk_rows, "entities": list(entity_rows.values()), "facts": fact_rows, "trees": list(trees.values()), "mounts": mounts, "source_mounts": source_mounts}


def _graph_from_plan(plan: dict[str, Any], summaries: dict[str, str] | None = None) -> MemoryGraph:
    summaries, now = summaries or {}, datetime.now(timezone.utc).isoformat()
    nodes, facts, sources = [], [], []
    for tree in plan["trees"]:
        for node in tree["nodes"].values():
            nodes.append({"node_id": node["id"], "tree_id": tree["id"], "tree_type": tree["tree_type"], "node_type": node["node_type"], "parent_id": node["parent_id"], "level": node["level"], "knowledge_base_id": plan["knowledge_base_id"], "organization_id": plan["organization_id"], "owner_user_id": plan["owner_user_id"], "subject_key": tree["subject_key"], "topic_key": node["topic_key"], "phase_key": node["phase_key"], "summary": summaries.get(node["id"]) or node["title"], "summary_version": 1, "summary_strategy_version": settings.memory_summary_strategy_version, "content_version": plan["content_version"], "time_start": plan["observed_at"], "time_end": plan["observed_at"], "visibility": plan["visibility"], "access_scope": plan["access_scope"], "sensitivity": plan["sensitivity"], "auth_object_key": plan["auth_object_key"], "acl_version": plan["acl_version"], "embedding_model": settings.embedding_model, "mapping_version": "v1", "lifecycle_status": "active", "created_at": now, "indexed_at": now})
    for mount in plan["mounts"]:
        row, tree, fact = mount["fact"], mount["tree"], mount["fact"]["fact"]
        # Projection identity is stable across Fact versions, so a new current
        # version overwrites the old ES document instead of leaving stale hits.
        facts.append({"fact_projection_id": _stable_uuid("projection", row["id"], mount["leaf_id"]), "fact_id": row["id"], "fact_version_id": row["version_id"], "tree_id": tree["id"], "tree_type": tree["tree_type"], "node_id": mount["leaf_id"], "parent_id": mount["leaf_id"], "level": 3, "node_type": "fact", "knowledge_base_id": plan["knowledge_base_id"], "organization_id": plan["organization_id"], "owner_user_id": plan["owner_user_id"], "subject_entity_id": row["entity_id"], "fact_type": fact.fact_type, "fact_text": fact.text, "dedupe_key": fact.dedupe_key, "fact_status": "active", "valid_from": fact.occurred_at, "observed_at": plan["observed_at"], "conversation_group_id": plan["conversation_group_id"], "message_id": plan["message_id"], "source_chunk_ids": list(fact.chunk_ids), "knowledge_item_id": plan["knowledge_item_id"], "attachment_id": plan["attachment_id"], "visibility": plan["visibility"], "access_scope": plan["access_scope"], "sensitivity": plan["sensitivity"], "auth_object_key": plan["auth_object_key"], "acl_version": plan["acl_version"], "content_version": plan["content_version"], "embedding_model": settings.embedding_model, "mapping_version": "v1", "lifecycle_status": "active", "created_at": now, "indexed_at": now})
    for mount in plan.get("source_mounts", []):
        route, tree = mount["route"], mount["tree"]
        sources.append({
            "node_id": mount["leaf_id"], "tree_id": tree["id"], "tree_type": tree["tree_type"],
            "source_id": plan["source_id"], "source_type": plan["source_type"],
            "source_resource_id": str(plan["source_resource_id"]), "knowledge_item_id": plan["knowledge_item_id"],
            "attachment_id": plan["attachment_id"], "relation_type": route.relation_type,
            "relevance_score": route.confidence, "content_version": plan["content_version"],
            "acl_version": plan["acl_version"], "visibility": plan["visibility"],
            "auth_object_key": plan["auth_object_key"], "status": "active",
            "observed_at": plan["observed_at"],
            "source_metadata": {
                "processing_status": plan["processing_status"],
                **plan["processing_error"],
            },
        })
    return MemoryGraph(plan["source_id"], plan["knowledge_item_id"], tuple(nodes), tuple(facts), tuple(sources))


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
                    # A remote PostgreSQL restart can leave a pooled
                    # connection unusable. Do not let rollback mask the real
                    # error, and discard the pool so the next request builds
                    # fresh connections instead of reusing a dead socket.
                    try:
                        pooled.rollback()
                    except Exception:
                        pass
                    try:
                        if self._pool is not None:
                            self._pool.close()
                    finally:
                        self._pool = None
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
                cursor.execute(f"SELECT id::text,status,retry_count FROM {self.schema}.processing_jobs WHERE source_event_id=%s::uuid", (source_event_id,))
                row = cursor.fetchone()
        return {"id": str(row[0]), "status": str(row[1]), "retry_count": int(row[2] or 0)} if row else None

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
                    (knowledge_item_id,organization_id,content_version,acl_version,content_variant,es_index_alias,es_document_prefix,chunk_count,mapping_version,status,indexed_at,updated_at,object_type)
                    VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='ready' THEN CURRENT_TIMESTAMP ELSE NULL END,CURRENT_TIMESTAMP,'chunk')
                    ON CONFLICT (knowledge_item_id,content_version,content_variant) WHERE object_type='chunk' DO UPDATE SET
                      acl_version=EXCLUDED.acl_version,es_index_alias=EXCLUDED.es_index_alias,
                      es_document_prefix=EXCLUDED.es_document_prefix,chunk_count=EXCLUDED.chunk_count,
                      mapping_version=EXCLUDED.mapping_version,status=EXCLUDED.status,
                      indexed_at=EXCLUDED.indexed_at,updated_at=CURRENT_TIMESTAMP""",
                    (knowledge_item_id, organization_id, content_version, acl_version, content_variant, es_index_alias, es_document_prefix, chunk_count, mapping_version, status, status),
                )

    def upsert_memory(self, context: AttachmentContext, chunks: list[ChunkRecord], facts: list[FactCandidate], *, routes: list[SourceRouteCandidate] | None = None, processing_status: str = "ready", processing_error: dict[str, Any] | None = None) -> MemoryGraph:
        plan = _memory_plan(context, chunks, facts, routes, processing_status, processing_error)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""INSERT INTO {self.schema}.memory_sources
                    (id,source_type,source_resource_id,knowledge_item_id,message_id,attachment_id,conversation_key,observed_at,knowledge_base_id,organization_id,owner_user_id,source_ref,content_version,content_hash,acl_version,visibility,access_scope,sensitivity,auth_object_key,processing_version)
                    VALUES (%s::uuid,%s,%s,%s::uuid,%s::uuid,%s::uuid,%s,%s::timestamptz,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_type,source_resource_id,content_version,processing_version) DO UPDATE SET
                      knowledge_item_id=EXCLUDED.knowledge_item_id,
                      knowledge_base_id=EXCLUDED.knowledge_base_id,
                      organization_id=EXCLUDED.organization_id,
                      owner_user_id=EXCLUDED.owner_user_id,
                      message_id=EXCLUDED.message_id,
                      attachment_id=EXCLUDED.attachment_id,
                      conversation_key=EXCLUDED.conversation_key,
                      observed_at=EXCLUDED.observed_at,
                      content_hash=EXCLUDED.content_hash,
                      acl_version=EXCLUDED.acl_version,
                      visibility=EXCLUDED.visibility,
                      access_scope=EXCLUDED.access_scope,
                      sensitivity=EXCLUDED.sensitivity,
                      auth_object_key=EXCLUDED.auth_object_key,
                      updated_at=CURRENT_TIMESTAMP""",
                    (plan["source_id"], plan["source_type"], str(plan["source_resource_id"]), plan["knowledge_item_id"], _uuid(context.message_id, "message", context.message_id) if context.message_id else None, _uuid(context.attachment_id, "attachment", context.attachment_id) if context.attachment_id else None, context.conversation_group_id or context.external_conversation_id, plan["observed_at"], plan["knowledge_base_id"], plan["organization_id"], plan["owner_user_id"], context.object_ref, context.content_version, plan["source_hash"], context.acl_version, plan["visibility"], context.access_scope, context.sensitivity, plan["auth_object_key"], settings.memory_extraction_version))
                for row in plan["chunks"]:
                    chunk = row["chunk"]
                    cursor.execute(f"""INSERT INTO {self.schema}.memory_chunks
                        (id,external_chunk_id,source_id,knowledge_item_id,attachment_id,content_version,chunk_index,chunk_count,chunk_text,content_hash,chunking_version,source_locator,visibility,access_scope,sensitivity,auth_object_key,acl_version,embedding_status)
                        VALUES (%s::uuid,%s,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (external_chunk_id) DO UPDATE SET chunk_text=EXCLUDED.chunk_text,content_hash=EXCLUDED.content_hash,source_locator=EXCLUDED.source_locator,updated_at=CURRENT_TIMESTAMP""",
                        (row["id"], row["external_chunk_id"], plan["source_id"], plan["knowledge_item_id"], _uuid(chunk.attachment_id, "attachment", chunk.attachment_id) if chunk.attachment_id else None, chunk.content_version, chunk.chunk_index, chunk.chunk_count, chunk.content, chunk.content_hash, chunk.chunking_version, json.dumps(chunk.source_locator, ensure_ascii=False), chunk.content_visibility, chunk.access_scope, chunk.sensitivity, chunk.auth_object_key, chunk.auth_acl_version, "ready" if chunk.vectorized else "pending"))
                for row in plan["entities"]:
                    candidate = row.get("fact") or row.get("route")
                    cursor.execute(f"""INSERT INTO {self.schema}.memory_entities
                        (id,knowledge_base_id,organization_id,owner_user_id,entity_type,canonical_name,normalized_key,confidence,recognition_version)
                        VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s)
                        ON CONFLICT (knowledge_base_id,entity_type,normalized_key) DO UPDATE SET canonical_name=EXCLUDED.canonical_name,confidence=EXCLUDED.confidence,updated_at=CURRENT_TIMESTAMP""",
                        (row["id"], plan["knowledge_base_id"], plan["organization_id"], plan["owner_user_id"], candidate.entity_type, candidate.subject, row["normalized_key"], candidate.confidence, settings.memory_extraction_version))
                for row in plan["facts"]:
                    fact = row["fact"]
                    cursor.execute(f"""INSERT INTO {self.schema}.memory_facts
                        (id,knowledge_base_id,organization_id,owner_user_id,fact_type,dedupe_key,subject_entity_id,predicate,visibility,access_scope,sensitivity,auth_object_key,acl_version)
                        VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (knowledge_base_id,dedupe_key) DO UPDATE SET updated_at=CURRENT_TIMESTAMP RETURNING id::text""",
                        (row["id"], plan["knowledge_base_id"], plan["organization_id"], plan["owner_user_id"], fact.fact_type, fact.dedupe_key, row["entity_id"], fact.predicate, plan["visibility"], context.access_scope, context.sensitivity, plan["auth_object_key"], context.acl_version))
                    row["id"] = cursor.fetchone()[0]
                    cursor.execute(f"SELECT id::text,version_no,fact_text FROM {self.schema}.memory_fact_versions WHERE fact_id=%s::uuid ORDER BY version_no DESC LIMIT 1", (row["id"],))
                    latest = cursor.fetchone()
                    if latest and latest[2] == fact.text:
                        row["version_id"] = latest[0]
                    else:
                        version_no = int(latest[1]) + 1 if latest else 1
                        row["version_id"] = _stable_uuid("fact-version", row["id"], version_no, hashlib.sha256(fact.text.encode()).hexdigest())
                        cursor.execute(f"""INSERT INTO {self.schema}.memory_fact_versions
                            (id,fact_id,version_no,fact_text,normalized_value,valid_from,observed_at,status,supersedes_version_id,confidence,extraction_version,content_version,visibility,access_scope,sensitivity,auth_object_key,acl_version)
                            VALUES (%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s::timestamptz,%s::timestamptz,'active',%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (row["version_id"], row["id"], version_no, fact.text, json.dumps(fact.normalized_value, ensure_ascii=False), fact.occurred_at, plan["observed_at"], latest[0] if latest else None, fact.confidence, settings.memory_extraction_version, context.content_version, plan["visibility"], context.access_scope, context.sensitivity, plan["auth_object_key"], context.acl_version))
                        if latest:
                            cursor.execute(f"UPDATE {self.schema}.memory_fact_versions SET status='superseded',valid_to=CURRENT_TIMESTAMP WHERE id=%s::uuid", (latest[0],))
                        cursor.execute(f"UPDATE {self.schema}.memory_facts SET current_version_id=%s::uuid WHERE id=%s::uuid", (row["version_id"], row["id"]))
                    for chunk_id in row["chunk_ids"]:
                        cursor.execute(f"""INSERT INTO {self.schema}.memory_fact_chunks (fact_id,fact_version_id,chunk_id,evidence_text,confidence)
                            VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s) ON CONFLICT DO NOTHING""", (row["id"], row["version_id"], chunk_id, fact.text, fact.confidence))
                for tree in plan["trees"]:
                    cursor.execute(f"""INSERT INTO {self.schema}.memory_trees
                        (id,knowledge_base_id,organization_id,owner_user_id,tree_type,subject_key,strategy_version)
                        VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s)
                        ON CONFLICT (knowledge_base_id,tree_type,subject_key) DO UPDATE SET updated_at=CURRENT_TIMESTAMP RETURNING id::text""",
                        (tree["id"], plan["knowledge_base_id"], plan["organization_id"], plan["owner_user_id"], tree["tree_type"], tree["subject_key"], settings.memory_tree_strategy_version))
                    tree["id"] = cursor.fetchone()[0]
                    for node in sorted(tree["nodes"].values(), key=lambda value: value["level"]):
                        cursor.execute(f"""INSERT INTO {self.schema}.memory_nodes
                            (id,tree_id,parent_id,node_type,level,node_key,topic_key,phase_key,time_start,time_end,dirty)
                            VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s::timestamptz,%s::timestamptz,TRUE)
                            ON CONFLICT (tree_id,node_key) DO UPDATE SET time_start=EXCLUDED.time_start,time_end=EXCLUDED.time_end,dirty=TRUE,updated_at=CURRENT_TIMESTAMP RETURNING id::text""",
                            (node["id"], tree["id"], node["parent_id"], node["node_type"], node["level"], node["node_key"], node["topic_key"], node["phase_key"], node.get("time_start"), node.get("time_end")))
                        node["id"] = cursor.fetchone()[0]
                    cursor.execute(f"UPDATE {self.schema}.memory_trees SET root_node_id=%s::uuid WHERE id=%s::uuid", (tree["root_id"], tree["id"]))
                for mount in plan["mounts"]:
                    cursor.execute(f"""UPDATE {self.schema}.memory_node_facts SET status='removed'
                        WHERE node_id=%s::uuid AND fact_id=%s::uuid AND fact_version_id<>%s::uuid AND status='active'""",
                        (mount["leaf_id"], mount["fact"]["id"], mount["fact"]["version_id"]))
                    cursor.execute(f"""INSERT INTO {self.schema}.memory_node_facts
                        (node_id,fact_id,fact_version_id,mount_strategy_version) VALUES (%s::uuid,%s::uuid,%s::uuid,%s)
                        ON CONFLICT (node_id,fact_version_id) DO UPDATE SET status='active'""",
                        (mount["leaf_id"], mount["fact"]["id"], mount["fact"]["version_id"], settings.memory_tree_strategy_version))
                source_mounts = plan.get("source_mounts", [])
                if processing_status == "ready":
                    active_node_ids = [mount["leaf_id"] for mount in source_mounts]
                    cursor.execute(
                        f"""UPDATE {self.schema}.memory_node_sources
                        SET status='removed',updated_at=CURRENT_TIMESTAMP
                        WHERE source_id=%s::uuid AND status='active'
                          AND NOT (node_id=ANY(%s::uuid[]))""",
                        (plan["source_id"], active_node_ids),
                    )
                for mount in source_mounts:
                    route = mount["route"]
                    cursor.execute(f"""INSERT INTO {self.schema}.memory_node_sources
                        (node_id,source_id,relation_type,relevance_score,source_metadata,content_version,visibility,access_scope,sensitivity,auth_object_key,acl_version,mount_strategy_version,observed_at,status)
                        VALUES (%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::timestamptz,'active')
                        ON CONFLICT (node_id,source_id) DO UPDATE SET
                          relation_type=EXCLUDED.relation_type,relevance_score=EXCLUDED.relevance_score,
                          source_metadata=EXCLUDED.source_metadata,
                          mount_strategy_version=EXCLUDED.mount_strategy_version,observed_at=EXCLUDED.observed_at,status='active',updated_at=CURRENT_TIMESTAMP""",
                        (mount["leaf_id"], plan["source_id"], route.relation_type, route.confidence,
                         json.dumps({"processing_status": plan["processing_status"], **plan["processing_error"]}, ensure_ascii=False),
                         plan["content_version"], plan["visibility"], plan["access_scope"], plan["sensitivity"],
                         plan["auth_object_key"], plan["acl_version"], settings.memory_tree_strategy_version, plan["observed_at"]))
        self._last_memory_plan = plan
        return _graph_from_plan(plan)

    def update_node_summaries(self, summaries: dict[str, str], *, strategy_version: str) -> tuple[dict[str, Any], ...]:
        if not summaries:
            return ()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for node_id, summary in summaries.items():
                    cursor.execute(f"UPDATE {self.schema}.memory_nodes SET display_summary=%s,summary_version=summary_version+1,summary_strategy_version=%s,dirty=FALSE,updated_at=CURRENT_TIMESTAMP WHERE id=%s::uuid", (summary, strategy_version, node_id))
        plan = getattr(self, "_last_memory_plan", None)
        return _graph_from_plan(plan, summaries).nodes if plan else ()

    def evidence_for_facts(self, fact_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not fact_ids:
            return {}
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""SELECT fc.fact_id::text,c.external_chunk_id,c.chunk_text,c.knowledge_item_id::text,c.attachment_id::text,c.source_locator,
                    s.source_type,s.source_resource_id,s.conversation_key,s.observed_at,s.message_id::text,s.knowledge_base_id::text,s.organization_id::text
                    FROM {self.schema}.memory_fact_chunks fc
                    JOIN {self.schema}.memory_chunks c ON c.id=fc.chunk_id
                    JOIN {self.schema}.memory_facts f ON f.id=fc.fact_id AND f.current_version_id=fc.fact_version_id
                    JOIN {self.schema}.memory_sources s ON s.id=c.source_id
                    WHERE fc.fact_id=ANY(%s::uuid[]) ORDER BY c.chunk_index""", (fact_ids,))
                rows = cursor.fetchall()
        output: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            output.setdefault(row[0], []).append({
                "chunk_id": row[1], "content": row[2], "knowledge_item_id": row[3],
                "attachment_id": row[4], "source_locator": row[5] or {},
                "source_type": row[6], "source_resource_id": row[7],
                "conversation_key": row[8], "observed_at": row[9], "message_id": row[10], "knowledge_base_id": row[11],
                "organization_id": row[12],
            })
        return output

    def sources_for_nodes(self, node_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not node_ids:
            return {}
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""SELECT ns.node_id::text,s.id::text,s.source_type,s.source_resource_id,
                    s.knowledge_item_id::text,s.attachment_id::text,s.content_version,s.acl_version,
                    s.visibility,s.auth_object_key,ns.relation_type,ns.relevance_score,ns.source_metadata,ns.observed_at
                    FROM {self.schema}.memory_node_sources ns
                    JOIN {self.schema}.memory_sources s ON s.id=ns.source_id
                    WHERE ns.node_id=ANY(%s::uuid[]) AND ns.status='active'""", (node_ids,))
                rows = cursor.fetchall()
        output: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            output.setdefault(row[0], []).append({
                "source_id": row[1], "source_type": row[2], "source_resource_id": row[3],
                "knowledge_item_id": row[4], "attachment_id": row[5], "content_version": row[6],
                "acl_version": row[7], "visibility": row[8], "auth_object_key": row[9],
                "relation_type": row[10], "relevance_score": float(row[11] or 0), "source_metadata": row[12] or {}, "observed_at": row[13],
            })
        return output

    def record_memory_projections(self, graph: MemoryGraph, *, embedding_model: str) -> None:
        rows = [("node", row, "node_id") for row in graph.nodes] + [("fact", row, "fact_projection_id") for row in graph.fact_projections]
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for object_type, row, document_field in rows:
                    document_id = str(row[document_field])
                    object_id = str(row["node_id"] if object_type == "node" else row["fact_id"])
                    visibility = "protected" if row.get("visibility") == "protected" else "display"
                    alias = {
                        ("node", "display"): settings.memory_display_nodes_index,
                        ("node", "protected"): settings.memory_protected_nodes_index,
                        ("fact", "display"): settings.memory_display_facts_index,
                        ("fact", "protected"): settings.memory_protected_facts_index,
                    }[(object_type, visibility)]
                    cursor.execute(f"""INSERT INTO {self.schema}.index_records
                        (knowledge_item_id,organization_id,owner_user_id,content_version,acl_version,content_variant,es_index_alias,es_document_prefix,chunk_count,mapping_version,status,indexed_at,object_type,object_id,es_document_id,embedding_model)
                        VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,0,%s,'ready',CURRENT_TIMESTAMP,%s,%s::uuid,%s,%s)
                        ON CONFLICT (object_type,object_id,es_document_id,content_variant,content_version,mapping_version) WHERE object_type IN ('fact','node')
                        DO UPDATE SET es_index_alias=EXCLUDED.es_index_alias,status='ready',indexed_at=CURRENT_TIMESTAMP,embedding_model=EXCLUDED.embedding_model,updated_at=CURRENT_TIMESTAMP""",
                        (graph.knowledge_item_id, row.get("organization_id"), row.get("owner_user_id"), int(row.get("content_version") or 1), int(row.get("acl_version") or 0), visibility, alias, document_id, str(row.get("mapping_version") or "v1"), object_type, object_id, document_id, embedding_model))

    def record_search(self, *, user_id: str, organization_id: str | None, query_text: str, query_hash: str, filters: dict[str, Any], result_count: int, duration_ms: int, request_id: str | None) -> None:
        stored_user_id = _uuid(user_id, "qa-user", user_id)
        stored_organization_id = _uuid(organization_id, "qa-org", organization_id) if organization_id else None
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.search_history
                    (user_id,organization_id,query_text,query_hash,filters,result_count,duration_ms,request_id)
                    VALUES (%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s,%s,%s)""",
                    (stored_user_id, stored_organization_id, query_text, query_hash, json.dumps(filters, ensure_ascii=False), result_count, duration_ms, request_id),
                )

    def add_outbox_event(self, envelope: dict[str, Any], *, aggregate_type: str, aggregate_id: str, event_version: int = 1) -> str:
        event_id = str(envelope.get("event_id") or uuid.uuid4())
        event_type = str(envelope.get("event_type") or "")
        # The worker may assign a version that is derived from the callback
        # identity.  Prefer the envelope value so PostgreSQL's aggregate /
        # event-version uniqueness constraint does not collapse distinct
        # callback attempts to the default value of one.
        event_version = int(envelope.get("event_version") or event_version)
        if event_type not in {"document.extracted", "processing.completed", "processing.failed", "knowledge.rag.processing", "knowledge.rag.succeeded", "knowledge.rag.failed"}:
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
                cursor.execute(f"""SELECT id::text,event_type,event_version,schema_version,organization_id,trace_id,payload
                    FROM {self.schema}.outbox_events
                    WHERE status IN ('pending','failed') AND available_at <= CURRENT_TIMESTAMP
                    ORDER BY created_at LIMIT %s""", (max(1, limit),))
                rows = cursor.fetchall()
        return [{"event_id": row[0], "event_type": row[1], "event_version": row[2], "schema_version": row[3], "organization_id": str(row[4]) if row[4] else None, "trace_id": row[5], "producer": settings.service_name, "payload": row[6] or {}} for row in rows]

    def mark_outbox_published(self, event_id: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {self.schema}.outbox_events SET status='published',published_at=CURRENT_TIMESTAMP WHERE id=%s::uuid", (event_id,))

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.outbox_events
                    SET status='failed',retry_count=retry_count+1,last_error=%s,
                        available_at=CURRENT_TIMESTAMP + INTERVAL '30 seconds'
                    WHERE id=%s::uuid""",
                    (str(error)[:1000], event_id),
                )

    def create_qa_conversation(self, *, user_id: str, organization_id: str | None, title: str | None = None, retrieval_mode: str = "quick", knowledge_base_ids: list[str] | None = None) -> str:
        # The database schema keeps user and organization IDs as UUIDs. Core
        # normally supplies UUIDs, while local development may use a stable
        # label such as ``dev-user``; map only that non-production label to a
        # deterministic UUID so the API remains usable without weakening the
        # ownership predicate.
        stored_user_id = _uuid(user_id, "qa-user", user_id)
        stored_organization_id = _uuid(organization_id, "qa-org", organization_id) if organization_id else None
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {self.schema}.qa_conversations (user_id,organization_id,title,retrieval_mode,knowledge_base_ids) VALUES (%s::uuid,%s::uuid,%s,%s,%s::jsonb) RETURNING id::text", (stored_user_id, stored_organization_id, title, retrieval_mode if retrieval_mode in {"quick", "deep"} else "quick", json.dumps(knowledge_base_ids or [], ensure_ascii=False)))
                row = cursor.fetchone()
        return str(row[0])

    def list_qa_conversations(self, *, user_id: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        page, page_size = max(1, page), min(100, max(1, page_size))
        stored_user_id = _uuid(user_id, "qa-user", user_id)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {self.schema}.qa_conversations WHERE user_id=%s::uuid AND status='active' AND deleted_at IS NULL", (stored_user_id,))
                total = int(cursor.fetchone()[0] or 0)
                cursor.execute(f"SELECT id::text,title,retrieval_mode,knowledge_base_ids,created_at,updated_at FROM {self.schema}.qa_conversations WHERE user_id=%s::uuid AND status='active' AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT %s OFFSET %s", (stored_user_id, page_size, (page - 1) * page_size))
                conversations = cursor.fetchall()
                output = []
                for row in conversations:
                    cursor.execute(f"SELECT COUNT(*) FROM {self.schema}.qa_messages WHERE conversation_id=%s::uuid", (row[0],))
                    updated_at = row[5].isoformat() if row[5] else None
                    output.append({"id": str(row[0]), "title": row[1] or "新的对话", "retrieval_mode": row[2] or "quick", "knowledge_base_ids": row[3] or [], "created_at": row[4].isoformat() if row[4] else None, "updated_at": updated_at, "last_message_at": updated_at, "message_count": int(cursor.fetchone()[0] or 0)})
        return output, total

    def get_qa_conversation(self, *, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        stored_user_id = _uuid(user_id, "qa-user", user_id)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT id::text,title,retrieval_mode,knowledge_base_ids,created_at,updated_at FROM {self.schema}.qa_conversations WHERE id=%s::uuid AND user_id=%s::uuid AND status='active' AND deleted_at IS NULL", (conversation_id, stored_user_id))
                row = cursor.fetchone()
                if not row: return None
                cursor.execute(f"SELECT id::text,role,content,citations,model_name,prompt_version,status,token_usage,duration_ms,error_message,created_at FROM {self.schema}.qa_messages WHERE conversation_id=%s::uuid ORDER BY created_at", (conversation_id,))
                messages = cursor.fetchall()
        return {"id": str(row[0]), "title": row[1] or "新的对话", "retrieval_mode": row[2] or "quick", "knowledge_base_ids": row[3] or [], "created_at": row[4].isoformat() if row[4] else None, "updated_at": row[5].isoformat() if row[5] else None, "messages": [{"id": str(value[0]), "role": value[1], "content": value[2], "citations": value[3] or [], "model_name": value[4], "prompt_version": value[5], "status": value[6], "token_usage": value[7] or {}, "duration_ms": value[8], "error_message": value[9], "created_at": value[10].isoformat() if value[10] else None} for value in messages]}

    def rename_qa_conversation(self, *, user_id: str, conversation_id: str, title: str) -> bool:
        stored_user_id = _uuid(user_id, "qa-user", user_id)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {self.schema}.qa_conversations SET title=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s::uuid AND user_id=%s::uuid AND status='active' AND deleted_at IS NULL", (title[:300], conversation_id, stored_user_id))
                return cursor.rowcount == 1

    def delete_qa_conversation(self, *, user_id: str, conversation_id: str) -> bool:
        stored_user_id = _uuid(user_id, "qa-user", user_id)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {self.schema}.qa_conversations SET status='deleted',deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=%s::uuid AND user_id=%s::uuid AND status='active' AND deleted_at IS NULL", (conversation_id, stored_user_id))
                return cursor.rowcount == 1

    def add_qa_message(self, *, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]] | None = None, model_name: str | None = None, prompt_version: str | None = None, status: str = "completed", token_usage: dict[str, Any] | None = None, duration_ms: int | None = None, error_message: str | None = None) -> str:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {self.schema}.qa_messages (conversation_id,role,content,citations,model_name,prompt_version,status,token_usage,duration_ms,error_message) VALUES (%s::uuid,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id::text", (conversation_id, role, content, json.dumps(citations or [], ensure_ascii=False), model_name, prompt_version, status, json.dumps(token_usage or {}, ensure_ascii=False), duration_ms, error_message))
                row = cursor.fetchone()
                cursor.execute(f"UPDATE {self.schema}.qa_conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=%s::uuid", (conversation_id,))
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
        self.memory_plans: dict[str, dict[str, Any]] = {}
        self.memory_graphs: dict[str, MemoryGraph] = {}
        self.memory_current_versions: dict[str, str] = {}

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

    def add_outbox_event(self, envelope: dict[str, Any], *, aggregate_type: str = "", aggregate_id: str = "", event_version: int = 1) -> str:
        event_id = str(envelope.get("event_id") or uuid.uuid4())
        event_type = str(envelope.get("event_type") or "")
        payload = envelope.get("payload") or {}
        aggregate_id = str(aggregate_id or payload.get("knowledge_item_id") or "")
        event_version = int(envelope.get("event_version") or event_version or 1)
        for existing in self.outbox_events:
            if (
                existing.get("aggregate_type") == aggregate_type
                and str(existing.get("aggregate_id") or "") == aggregate_id
                and existing.get("event_type") == event_type
                and int(existing.get("event_version") or 1) == event_version
            ):
                return str(existing.get("event_id") or existing.get("id"))
        if event_type.startswith("knowledge.rag."):
            for existing in self.outbox_events:
                existing_payload = existing.get("payload") or {}
                if (
                    existing.get("event_type") == event_type
                    and str(existing_payload.get("knowledge_item_id") or "") == aggregate_id
                    and str(existing_payload.get("source_event_id") or "") == str(payload.get("source_event_id") or "")
                    and str(existing_payload.get("rag_job_id") or "") == str(payload.get("rag_job_id") or "")
                ):
                    return str(existing.get("event_id") or existing.get("id"))
        self.outbox_events.append({"id": event_id, "status": "pending", "aggregate_type": aggregate_type, "aggregate_id": aggregate_id, "event_version": event_version, **envelope})
        return event_id

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [{key: value for key, value in item.items() if key != "id" and key != "status"} for item in self.outbox_events if item.get("status") in {"pending", "failed"}][: max(1, limit)]

    def mark_outbox_published(self, event_id: str) -> None:
        for item in self.outbox_events:
            if item.get("event_id") == event_id or item.get("id") == event_id:
                item["status"] = "published"

    def mark_outbox_failed(self, event_id: str, error: str) -> None:
        for item in self.outbox_events:
            if item.get("event_id") == event_id or item.get("id") == event_id:
                item["status"] = "failed"
                item["last_error"] = str(error)[:1000]
                item["retry_count"] = int(item.get("retry_count") or 0) + 1

    def create_qa_conversation(self, *, user_id: str, organization_id: str | None, title: str | None = None, retrieval_mode: str = "quick", knowledge_base_ids: list[str] | None = None) -> str:
        value = str(uuid.uuid4())
        self.qa_conversations.append({"id": value, "user_id": user_id, "organization_id": organization_id, "title": title or "新的对话", "retrieval_mode": retrieval_mode, "knowledge_base_ids": knowledge_base_ids or [], "status": "active", "deleted_at": None})
        return value

    def add_qa_message(self, *, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]] | None = None, model_name: str | None = None, prompt_version: str | None = None, status: str = "completed", token_usage: dict[str, Any] | None = None, duration_ms: int | None = None, error_message: str | None = None) -> str:
        value = str(uuid.uuid4())
        self.qa_messages.append({"id": value, "conversation_id": conversation_id, "role": role, "content": content, "citations": citations or [], "model_name": model_name, "prompt_version": prompt_version, "status": status, "token_usage": token_usage or {}, "duration_ms": duration_ms, "error_message": error_message})
        for conversation in self.qa_conversations:
            if conversation.get("id") == conversation_id:
                conversation["updated_at"] = datetime.now(timezone.utc).isoformat()
        return value

    def list_qa_conversations(self, *, user_id: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        values = [item for item in self.qa_conversations if item.get("user_id") == user_id and item.get("status", "active") == "active" and not item.get("deleted_at")]
        total = len(values); start = max(0, page - 1) * max(1, page_size); values = values[start:start + min(100, max(1, page_size))]
        return ([{**item, "message_count": sum(1 for message in self.qa_messages if message.get("conversation_id") == item["id"])} for item in values], total)

    def get_qa_conversation(self, *, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        item = next((value for value in self.qa_conversations if str(value.get("id")) == str(conversation_id) and value.get("user_id") == user_id and value.get("status", "active") == "active" and not value.get("deleted_at")), None)
        return {**item, "messages": [dict(value) for value in self.qa_messages if str(value.get("conversation_id")) == str(conversation_id)]} if item else None

    def rename_qa_conversation(self, *, user_id: str, conversation_id: str, title: str) -> bool:
        item = next((value for value in self.qa_conversations if str(value.get("id")) == str(conversation_id) and value.get("user_id") == user_id and value.get("status", "active") == "active"), None)
        if not item: return False
        item["title"] = title[:300]; return True

    def delete_qa_conversation(self, *, user_id: str, conversation_id: str) -> bool:
        item = next((value for value in self.qa_conversations if str(value.get("id")) == str(conversation_id) and value.get("user_id") == user_id and value.get("status", "active") == "active"), None)
        if not item: return False
        item["status"], item["deleted_at"] = "deleted", "now"; return True

    def upsert_memory(self, context: AttachmentContext, chunks: list[ChunkRecord], facts: list[FactCandidate], *, routes: list[SourceRouteCandidate] | None = None, processing_status: str = "ready", processing_error: dict[str, Any] | None = None) -> MemoryGraph:
        plan = _memory_plan(context, chunks, facts, routes, processing_status, processing_error)
        graph = _graph_from_plan(plan)
        self.memory_plans[plan["source_id"]] = plan
        self.memory_graphs[plan["source_id"]] = graph
        for row in graph.fact_projections:
            self.memory_current_versions[str(row["fact_id"])] = str(row["fact_version_id"])
        return graph

    def update_node_summaries(self, summaries: dict[str, str], *, strategy_version: str) -> tuple[dict[str, Any], ...]:
        updated: list[dict[str, Any]] = []
        for source_id, plan in self.memory_plans.items():
            graph = _graph_from_plan(plan, summaries)
            self.memory_graphs[source_id] = graph
            updated.extend(node for node in graph.nodes if node["node_id"] in summaries)
        return tuple(updated)

    def evidence_for_facts(self, fact_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        wanted, output = set(fact_ids), {}
        for plan in self.memory_plans.values():
            chunks = {row["id"]: row["chunk"] for row in plan["chunks"]}
            for row in plan["facts"]:
                if row["id"] not in wanted:
                    continue
                if self.memory_current_versions.get(str(row["id"])) not in {None, str(row["version_id"])}:
                    continue
                for chunk_id in row["chunk_ids"]:
                    chunk = chunks[chunk_id]
                    output.setdefault(row["id"], []).append({
                        "chunk_id": chunk.chunk_id,
                        "content": chunk.content,
                        "knowledge_item_id": chunk.knowledge_item_id,
                        "attachment_id": chunk.attachment_id,
                        "source_locator": chunk.source_locator,
                        "source_type": plan.get("source_type"),
                        "source_resource_id": plan.get("source_resource_id"),
                        "conversation_key": plan.get("conversation_group_id"),
                        "observed_at": plan.get("observed_at"),
                        "message_id": plan.get("message_id"),
                        "knowledge_base_id": chunk.knowledge_base_id,
                        "organization_id": chunk.organization_id,
                    })
        return output

    def sources_for_nodes(self, node_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        wanted, output = set(node_ids), {}
        for graph in self.memory_graphs.values():
            for source in graph.source_projections:
                node_id = str(source["node_id"])
                if node_id in wanted:
                    output.setdefault(node_id, []).append(dict(source))
        return output

    def record_memory_projections(self, graph: MemoryGraph, *, embedding_model: str) -> None:
        rows = [("node", row, "node_id") for row in graph.nodes] + [("fact", row, "fact_projection_id") for row in graph.fact_projections]
        for object_type, row, document_field in rows:
            key = (object_type, str(row[document_field]))
            value = {"object_type": object_type, "object_id": row["node_id"] if object_type == "node" else row["fact_id"], "es_document_id": row[document_field], "embedding_model": embedding_model, "status": "ready"}
            existing = next((item for item in self.index_records if (item.get("object_type"), item.get("es_document_id")) == key), None)
            if existing:
                existing.update(value)
            else:
                self.index_records.append(value)


def _safe_schema(value: str) -> str:
    value = (value or "rag").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("RAG_DATABASE_SCHEMA must be a simple SQL identifier")
    return value
