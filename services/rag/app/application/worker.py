from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.application.memory.pipeline import MemoryPipeline
from app.application.ports import ProcessingInput, ProcessingOutput
from app.application.processing.chunking import build_chunks
from app.application.processing.preprocessor import DocumentPreprocessor
from app.application.processing.vectorization import vectorization_report
from app.config import settings
from app.domain.models import AttachmentContext, ParsedDocument
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.elasticsearch import ElasticsearchChunkStore
from app.infrastructure.memory_elasticsearch import ElasticsearchMemoryStore
from app.infrastructure.memory_models import OpenAICompatibleFactExtractor, OpenAICompatibleNodeSummarizer
from app.infrastructure.events.redis_streams import RedisStreamPublisher
from app.infrastructure.module2.knowledge_client import Module2KnowledgeClient
from app.infrastructure.module2.rag_callback import KnowledgeRAGCallbackClient
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository
from app.infrastructure.storage.artifacts import build_artifact_store


logger = logging.getLogger("rag.worker")


class SourceMetadataError(ValueError):
    """Raised when an attachment cannot be made processable from metadata."""

    code = "SOURCE_METADATA_INCOMPLETE"
    retryable = True


class RAGEventHandler:
    def __init__(self, *, preprocessor: DocumentPreprocessor | None = None, indexer: Any | None = None, knowledge: Any | None = None, repository: Any | None = None, publisher: Any | None = None, memory_pipeline: Any | None = None, callback: Any | None = None) -> None:
        self.indexer = indexer or ElasticsearchChunkStore()
        self.knowledge = knowledge or Module2KnowledgeClient()
        self.repository = repository or (PostgresRagRepository() if settings.database_url else InMemoryRagRepository())
        self.publisher = publisher
        self.callback = callback or KnowledgeRAGCallbackClient()
        if preprocessor is not None:
            self.preprocessor = preprocessor
        else:
            self.preprocessor = DocumentPreprocessor(artifact_store=build_artifact_store(), embedding_provider=EmbeddingClient())
        embedding_provider = getattr(self.preprocessor, "embedding_provider", None) or EmbeddingClient()
        self.memory_pipeline = memory_pipeline or MemoryPipeline(
            extractor=OpenAICompatibleFactExtractor(), summarizer=OpenAICompatibleNodeSummarizer(),
            repository=self.repository, indexer=ElasticsearchMemoryStore(), embedding_provider=embedding_provider,
        )

    def handle(self, envelope: dict[str, Any]) -> None:
        event_type = str(envelope.get("event_type") or "")
        if event_type != "knowledge.ready":
            return
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        job_type = "full_process"
        get_job = getattr(self.repository, "get_processing_job", None)
        existing = get_job(str(envelope.get("event_id") or "")) if callable(get_job) else None
        # A succeeded event is fully durable and can be ACKed immediately.
        # A processing row may be left by a crashed worker; resume the same
        # job so the Redis redelivery can finish the pipeline idempotently.
        if existing and existing.get("status") == "succeeded":
            self.flush_outbox()
            return
        job_id = self.repository.create_processing_job(envelope, job_type=job_type)
        if not job_id:
            return
        self.repository.update_processing_job(job_id, status="processing", current_stage="fetch")
        self._publish_callback(envelope, "processing", job_id, {"retryable": True})
        try:
            contexts = self._contexts(payload, organization_id=envelope.get("organization_id"))
            if not contexts:
                raise RuntimeError("event contained no processable attachment or content")
            total = 0
            vectorized_total = 0
            skipped_total = 0
            skip_reasons: dict[str, int] = {}
            memory_completed = False
            fact_ids: set[str] = set()
            tree_ids: set[str] = set()
            node_ids: set[str] = set()
            for context in contexts:
                # Mount the source before parsing so sparse/failed documents
                # remain visible in the tree with an explicit processing state.
                try:
                    self.memory_pipeline.process(
                        context, [], stage=lambda value: self.repository.update_processing_job(job_id, current_stage=value),
                        processing_status="processing",
                    )
                except Exception:
                    # Source visibility must not prevent the document pipeline
                    # from making its normal retry decision.
                    pass
                self.repository.update_processing_job(job_id, current_stage="parse")
                if context.content_access_required and not context.file_path and not context.object_ref:
                    metadata_context = replace(context, part_kind="attachment_metadata", content_access_required=False)
                    parsed = ParsedDocument("", [], "metadata", "v1", manifest={"metadata_only": True})
                    output = ProcessingOutput(metadata_context, parsed, build_chunks(parsed, metadata_context))
                    context = metadata_context
                elif context.part_kind == "message_display" and context.inline_text is not None:
                    output = self.preprocessor.process_text(context.inline_text, context, vectorize=True)
                else:
                    output = self.preprocessor.process(
                        ProcessingInput(attachment=context, processing_job_id=job_id, trace_id=envelope.get("trace_id")),
                        vectorize=True,
                    )
                manifest = output.parsed.manifest or {}
                self.repository.update_processing_job(
                    job_id,
                    parser_name=output.parsed.parser,
                    parser_version=output.parsed.parser_version,
                    parsed_artifact_ref=output.parsed.artifact_ref,
                    parsed_content_hash=str(manifest.get("artifact_hash") or "").removeprefix("sha256:") or None,
                    page_count=manifest.get("page_count"),
                    chunking_version=settings.chunking_version,
                    embedding_model=getattr(getattr(self.preprocessor, "embedding_provider", None), "model", None) if event_type == "knowledge.ready" else None,
                )
                self.repository.update_processing_job(job_id, current_stage="index")
                delete_older = getattr(self.indexer, "delete_older_versions", None)
                if callable(delete_older):
                    delete_older(
                        knowledge_item_id=context.knowledge_item_id or context.attachment_id,
                        content_version=context.content_version,
                    )
                total += self.indexer.index_chunks(output.chunks)
                # A chunk that reaches the index without a vector is still
                # findable by BM25 and silently invisible to kNN. Record the
                # reason per chunk so "recall dropped" is traceable to a count
                # rather than to an absence of logs.
                report = vectorization_report(output.chunks)
                vectorized_total += report.vectorized
                skipped_total += report.skipped
                for reason, count in report.reasons().items():
                    if count:
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + count
                if report.omitted:
                    logger.warning(
                        "chunks left unvectorized although eligible: job_id=%s attachment_id=%s omitted=%d of %d",
                        job_id, context.attachment_id, report.omitted, report.total,
                    )
                self.repository.upsert_index_record(
                    knowledge_item_id=context.knowledge_item_id or context.attachment_id,
                    organization_id=context.organization_id,
                    content_version=context.content_version,
                    acl_version=context.acl_version,
                    content_variant="protected" if any(chunk.protected for chunk in output.chunks) else "display",
                    es_index_alias=settings.elasticsearch_protected_index if any(chunk.protected for chunk in output.chunks) else settings.elasticsearch_display_index,
                    es_document_prefix=output.chunks[0].chunk_id[:16] if output.chunks else context.attachment_id,
                    chunk_count=len(output.chunks),
                    mapping_version="v1",
                    status="ready",
                )
                memory_graph = self.memory_pipeline.process(
                    context, output.chunks, processing_status="ready",
                    stage=lambda value: self.repository.update_processing_job(job_id, current_stage=value),
                )
                memory_completed = memory_completed or memory_graph is not None
                if memory_graph is not None:
                    fact_ids.update(str(row.get("fact_id")) for row in memory_graph.fact_projections if row.get("fact_id"))
                    tree_ids.update(str(row.get("tree_id")) for row in memory_graph.fact_projections if row.get("tree_id"))
                    node_ids.update(str(row.get("node_id")) for row in memory_graph.nodes if row.get("node_id"))
            self.repository.update_processing_job(job_id, status="succeeded", current_stage="memory_index" if memory_completed else "index", finished_at=_now())
            self._publish_callback(envelope, "succeeded", job_id, {"chunk_count": total, "fact_count": len(fact_ids), "tree_count": len(tree_ids), "node_count": len(node_ids), "vectorized_chunk_count": vectorized_total, "skipped_chunk_count": skipped_total, "skip_reasons": skip_reasons, "retryable": False})
            if skipped_total:
                logger.warning(
                    "job_id=%s succeeded with partial vectorization: chunks=%d vectorized=%d skipped=%d reasons=%s",
                    job_id, total, vectorized_total, skipped_total, skip_reasons,
                )
            self._publish_result(envelope, "processing.completed", payload, {"processing_job_id": job_id, "chunk_count": total, "vectorized_chunk_count": vectorized_total, "skipped_chunk_count": skipped_total, "skip_reasons": skip_reasons, "parsed_artifact_ref": output.parsed.artifact_ref if 'output' in locals() else None})
        except Exception as exc:
            for failed_context in locals().get("contexts", []):
                try:
                    self.memory_pipeline.process(
                        failed_context, [], processing_status="failed",
                        processing_error={
                            "error_code": getattr(exc, "code", type(exc).__name__),
                            "error_message": str(exc)[:240],
                        },
                    )
                except Exception:
                    pass
            state = getattr(self.repository, "get_processing_job", lambda _: None)(str(envelope.get("event_id") or "")) or {}
            next_retry = int(state.get("retry_count") or 0) + 1
            terminal = next_retry >= max(1, settings.task_max_retries)
            self.repository.update_processing_job(job_id, status="failed", retry_count=next_retry, last_error=type(exc).__name__, finished_at=_now())
            if terminal:
                self._publish_callback(envelope, "failed", job_id, {"error_code": getattr(exc, "code", type(exc).__name__), "retryable": False})
            self._publish_result(envelope, "processing.failed", payload, {"processing_job_id": job_id, "error_code": getattr(exc, "code", type(exc).__name__), "retryable": bool(getattr(exc, "retryable", False))})
            # Once the terminal callback has been durably queued, acknowledge
            # the Redis entry. Further redelivery would repeat a terminal job
            # forever and create duplicate compatibility events.
            if terminal:
                return
            raise

    def flush_outbox(self, *, limit: int = 50) -> int:
        if self.publisher is None and self.callback is None:
            return 0
        published = 0
        for envelope in self.repository.pending_outbox(limit=limit):
            try:
                if str(envelope.get("event_type") or "").startswith("knowledge.rag."):
                    self.callback.send(envelope.get("payload") or {})
                elif self.publisher is not None:
                    self.publisher.publish(envelope)
                else:
                    continue
                self.repository.mark_outbox_published(str(envelope.get("event_id") or ""))
                published += 1
            except Exception as exc:
                # Keep callback delivery independent from parsing and apply a
                # bounded retry delay so an unavailable Knowledge service does
                # not cause a hot loop in the worker.
                mark_failed = getattr(self.repository, "mark_outbox_failed", None)
                if callable(mark_failed):
                    try:
                        mark_failed(str(envelope.get("event_id") or ""), type(exc).__name__)
                    except Exception:
                        pass
                continue
        return published

    def _publish_callback(self, source: dict[str, Any], status: str, job_id: str, extra: dict[str, Any]) -> None:
        if not settings.knowledge_callback_enabled or not settings.knowledge_base_url:
            return
        payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
        occurred_at = _now()
        callback_payload = {
            "knowledge_item_id": payload.get("knowledge_item_id"),
            "source_event_id": source.get("event_id"), "rag_job_id": job_id,
            "content_version": payload.get("content_version", 1), "acl_version": payload.get("acl_version", 0),
            "status": status, "occurred_at": occurred_at,
            # Knowledge binds this as a free-form object and stores it as JSONB,
            # so the skip breakdown rides along without a schema change on
            # either side. `vectorized_chunk_count` is the field that makes
            # "indexed but not searchable by vector" visible in the item's
            # own RAG result instead of only in the worker log.
            "result": {key: value for key, value in extra.items() if key in {"chunk_count", "fact_count", "tree_count", "node_count", "vectorized_chunk_count", "skipped_chunk_count", "skip_reasons"}},
            "error_code": extra.get("error_code"), "retryable": bool(extra.get("retryable", False)),
        }
        callback_event_id = str(uuid.uuid5(
            uuid.UUID("bcf50a0b-fc90-4c99-8d13-5a9ed4ac8f7f"),
            f"{source.get('event_id')}|{job_id}|{status}",
        ))
        # RAG outbox keeps an aggregate/event-version uniqueness constraint.
        # Encode the processing attempt in event_version so a new job for the
        # same Knowledge item is not suppressed by an older callback.
        callback_event_version = (uuid.UUID(callback_event_id).int % (2**63 - 1)) + 1
        envelope = {"event_id": callback_event_id, "event_type": f"knowledge.rag.{status}", "event_version": callback_event_version, "schema_version": 1, "occurred_at": occurred_at, "trace_id": str(source.get("trace_id") or uuid.uuid4()), "organization_id": source.get("organization_id"), "producer": settings.service_name, "payload": callback_payload}
        self.repository.add_outbox_event(
            envelope,
            aggregate_type="knowledge_item",
            aggregate_id=str(payload.get("knowledge_item_id") or uuid.uuid4()),
        )
        self.flush_outbox()

    def _contexts(self, payload: dict[str, Any], *, organization_id: str | None = None) -> list[AttachmentContext]:
        raw_attachments = payload.get("attachments")
        if isinstance(raw_attachments, list) and raw_attachments:
            base = {key: value for key, value in payload.items() if key != "attachments"}
            return [self._attachment_context({**base, "organization_id": base.get("organization_id") or organization_id, **item}) for item in raw_attachments if isinstance(item, dict)]
        if isinstance(payload.get("attachment"), dict):
            base = {key: value for key, value in payload.items() if key not in {"attachment", "attachments"}}
            return [self._attachment_context({**base, "organization_id": base.get("organization_id") or organization_id, **payload["attachment"]})]
        if payload.get("attachment_id") or payload.get("file_name") or payload.get("object_ref"):
            return [self._attachment_context({**payload, "organization_id": payload.get("organization_id") or organization_id})]
        knowledge_item_id = str(payload.get("knowledge_item_id") or "")
        if not knowledge_item_id:
            return []
        # A message knowledge item can expose related attachments in its
        # metadata. That does not make the message body attachment content.
        # Only an event that explicitly carries an attachment should enter the
        # attachment-content branch below.
        event_attachment_id = str(payload.get("attachment_id") or payload.get("source_attachment_id") or "").strip()
        knowledge = self.knowledge.get_knowledge(knowledge_item_id, content_version=payload.get("content_version"), acl_version=payload.get("acl_version"))
        attachments = knowledge.get("attachments") if isinstance(knowledge, dict) else None
        if event_attachment_id and isinstance(attachments, list) and attachments:
            base = {key: value for key, value in knowledge.items() if key != "attachments"}
            base.setdefault("organization_id", organization_id)
            contexts: list[AttachmentContext] = []
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                candidate = {
                    **base,
                    **item,
                    "knowledge_item_id": knowledge_item_id,
                    "content_version": payload.get("content_version", knowledge.get("content_version", 1)),
                    "acl_version": payload.get("acl_version", knowledge.get("acl_version", 0)),
                }
                context = self._attachment_context(candidate, fetch_attachment=False, fetch_knowledge=False)
                if not context.content_access_required:
                    detail = self.knowledge.get_attachment(
                        context.attachment_id,
                        content_version=context.content_version,
                        acl_version=context.acl_version,
                    )
                    context = AttachmentContext.from_mapping({**candidate, **detail})
                contexts.append(context)
            return contexts
        # Fetch item metadata as well as the versioned body. The content
        # endpoint is intentionally narrow and may omit the current
        # knowledge_base_id/organization metadata needed for indexing filters.
        metadata = self.knowledge.get_knowledge(
            knowledge_item_id,
            content_version=payload.get("content_version"),
            acl_version=payload.get("acl_version"),
        )
        content = self.knowledge.get_content(knowledge_item_id, content_version=payload.get("content_version"), acl_version=payload.get("acl_version"), content_variant=payload.get("content_variant") or "display")
        text = content.get("text") or content.get("content") or content.get("body") if isinstance(content, dict) else None
        if not isinstance(text, str) or not text.strip():
            return []
        variant = str(payload.get("content_variant") or content.get("content_variant") or "display")
        part_kind = "knowledge_original" if variant == "original" else "message_display"
        merged = {
            **payload,
            **(metadata if isinstance(metadata, dict) else {}),
            **(content if isinstance(content, dict) else {}),
        }
        return [AttachmentContext(
            attachment_id=None, knowledge_item_id=knowledge_item_id,
            file_name="", mime_type="", file_path=None, inline_text=text,
            content_version=int(payload.get("content_version") or content.get("content_version") or 1),
            acl_version=int(payload.get("acl_version") or content.get("acl_version") or 0),
            content_access_required=part_kind == "knowledge_original",
            organization_id=str(merged.get("organization_id") or organization_id) if (merged.get("organization_id") or organization_id) else None,
            knowledge_base_id=merged.get("knowledge_base_id"), knowledge_scope=merged.get("knowledge_scope"),
            access_scope=merged.get("access_scope"), title=merged.get("title"), owner_user_id=merged.get("owner_user_id"),
            conversation_group_id=merged.get("conversation_group_id"), conversation_ingestion_id=merged.get("conversation_ingestion_id"),
            external_conversation_id=merged.get("external_conversation_id"), message_id=merged.get("message_id") or merged.get("source_message_id"),
            external_message_id=merged.get("external_message_id"), sender_identity_id=merged.get("sender_identity_id"),
            sender_display_name=merged.get("sender_display_name"), sent_at=merged.get("sent_at"), sensitivity=merged.get("sensitivity"),
            part_kind=part_kind,
        )]

    def _attachment_context(
        self,
        value: dict[str, Any],
        *,
        fetch_attachment: bool = True,
        fetch_knowledge: bool = True,
    ) -> AttachmentContext:
        """Build an attachment context, hydrating sparse event metadata from Knowledge."""
        merged = dict(value)
        attachment_id = str(merged.get("attachment_id") or merged.get("id") or "")
        knowledge_item_id = str(merged.get("knowledge_item_id") or "")
        if knowledge_item_id and fetch_knowledge and hasattr(self.knowledge, "get_knowledge"):
            knowledge = self.knowledge.get_knowledge(
                knowledge_item_id,
                content_version=merged.get("content_version"),
                acl_version=merged.get("acl_version"),
            )
            if isinstance(knowledge, dict):
                knowledge_metadata = {key: item for key, item in knowledge.items() if key != "attachments"}
                attachment_metadata: dict[str, Any] = {}
                for item in knowledge.get("attachments") or []:
                    if isinstance(item, dict) and str(item.get("attachment_id") or item.get("id") or "") == attachment_id:
                        attachment_metadata = item
                        break
                # Knowledge owns organization, knowledge-base, version, ACL,
                # and attachment metadata. Event fields are only a transport
                # snapshot and may be incomplete or stale.
                merged = {**merged, **knowledge_metadata, **attachment_metadata}
                attachment_id = str(merged.get("attachment_id") or merged.get("id") or attachment_id)
        needs_attachment_detail = (
            not str(merged.get("file_name") or merged.get("name") or "").strip()
            or not (merged.get("object_ref") or merged.get("storage_key") or merged.get("file_path"))
        )
        if attachment_id and needs_attachment_detail and fetch_attachment and not _truthy(merged.get("content_access_required", False)):
            detail = self.knowledge.get_attachment(
                attachment_id,
                content_version=merged.get("content_version"),
                acl_version=merged.get("acl_version"),
            )
            if isinstance(detail, dict):
                merged = {**merged, **detail}
        if not str(merged.get("file_name") or merged.get("name") or "").strip():
            # A metadata-only event may not have a display name. Use a stable
            # MIME-derived fallback so parsers do not fail on an empty path.
            mime = str(merged.get("mime_type") or "application/octet-stream").split(";", 1)[0].lower()
            extension = {"application/pdf": "pdf", "text/plain": "txt", "text/markdown": "md", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"}.get(mime, "bin")
            if attachment_id:
                merged["file_name"] = f"attachment-{attachment_id}.{extension}"
        try:
            return AttachmentContext.from_mapping(merged)
        except ValueError as exc:
            if "file_name" in str(exc):
                raise SourceMetadataError("attachment metadata is missing file_name") from exc
            raise

    def _publish_result(self, source: dict[str, Any], event_type: str, payload: dict[str, Any], extra: dict[str, Any]) -> None:
        envelope = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "schema_version": 1,
            "occurred_at": _now(),
            "trace_id": str(source.get("trace_id") or uuid.uuid4()),
            "organization_id": source.get("organization_id"),
            "producer": settings.service_name,
            "payload": {"knowledge_item_id": payload.get("knowledge_item_id"), "content_version": payload.get("content_version", 1), "acl_version": payload.get("acl_version", 0), **extra},
        }
        self.repository.add_outbox_event(
            envelope,
            aggregate_type="knowledge_item",
            aggregate_id=str(payload.get("knowledge_item_id") or uuid.uuid4()),
            event_version=int(payload.get("content_version") or 1),
        )
        self.flush_outbox()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
