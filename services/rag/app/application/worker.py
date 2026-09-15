from __future__ import annotations

import tempfile
import shutil
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.application.ports import ProcessingInput, ProcessingOutput
from app.application.processing.chunking import build_chunks
from app.application.processing.preprocessor import DocumentPreprocessor
from app.config import settings
from app.domain.models import AttachmentContext, ParsedDocument
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.elasticsearch import ElasticsearchChunkStore
from app.infrastructure.events.redis_streams import RedisStreamPublisher
from app.infrastructure.module2.knowledge_client import Module2KnowledgeClient
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository
from app.infrastructure.storage.artifacts import build_artifact_store


class RAGEventHandler:
    def __init__(self, *, preprocessor: DocumentPreprocessor | None = None, indexer: Any | None = None, knowledge: Any | None = None, repository: Any | None = None, publisher: Any | None = None) -> None:
        self.indexer = indexer or ElasticsearchChunkStore()
        self.knowledge = knowledge or Module2KnowledgeClient()
        self.repository = repository or (PostgresRagRepository() if settings.database_url else InMemoryRagRepository())
        self.publisher = publisher
        self._inline_temp_dirs: list[Path] = []
        if preprocessor is not None:
            self.preprocessor = preprocessor
        else:
            self.preprocessor = DocumentPreprocessor(artifact_store=build_artifact_store(), embedding_provider=EmbeddingClient())

    def handle(self, envelope: dict[str, Any]) -> None:
        event_type = str(envelope.get("event_type") or "")
        if event_type != "knowledge.ready":
            return
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        job_type = "full_process"
        get_job = getattr(self.repository, "get_processing_job", None)
        existing = get_job(str(envelope.get("event_id") or "")) if callable(get_job) else None
        if existing and existing.get("status") in {"succeeded", "processing"}:
            self.flush_outbox()
            return
        job_id = self.repository.create_processing_job(envelope, job_type=job_type)
        if not job_id:
            return
        self.repository.update_processing_job(job_id, status="processing", current_stage="fetch")
        try:
            contexts = self._contexts(payload, organization_id=envelope.get("organization_id"))
            if not contexts:
                raise RuntimeError("event contained no processable attachment or content")
            total = 0
            for context in contexts:
                self.repository.update_processing_job(job_id, current_stage="parse")
                if context.content_access_required and not context.file_path and not context.object_ref:
                    metadata_context = replace(context, part_kind="attachment_metadata", content_access_required=False)
                    parsed = ParsedDocument("", [], "metadata", "v1", manifest={"metadata_only": True})
                    output = ProcessingOutput(metadata_context, parsed, build_chunks(parsed, metadata_context))
                    context = metadata_context
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
            self.repository.update_processing_job(job_id, status="succeeded", current_stage="index", finished_at=_now())
            self._publish_result(envelope, "processing.completed", payload, {"processing_job_id": job_id, "chunk_count": total, "parsed_artifact_ref": output.parsed.artifact_ref if 'output' in locals() else None})
        except Exception as exc:
            self.repository.update_processing_job(job_id, status="failed", last_error=type(exc).__name__, finished_at=_now())
            try:
                self._publish_result(envelope, "processing.failed", payload, {"processing_job_id": job_id, "error_code": getattr(exc, "code", type(exc).__name__), "retryable": bool(getattr(exc, "retryable", False))})
            finally:
                self._cleanup_inline_files()
            raise
        finally:
            self._cleanup_inline_files()

    def flush_outbox(self, *, limit: int = 50) -> int:
        if self.publisher is None:
            return 0
        published = 0
        for envelope in self.repository.pending_outbox(limit=limit):
            try:
                self.publisher.publish(envelope)
                self.repository.mark_outbox_published(str(envelope.get("event_id") or ""))
                published += 1
            except Exception:
                # Leave the row pending for the next bounded retry.
                continue
        return published

    def _contexts(self, payload: dict[str, Any], *, organization_id: str | None = None) -> list[AttachmentContext]:
        raw_attachments = payload.get("attachments")
        if isinstance(raw_attachments, list) and raw_attachments:
            base = {key: value for key, value in payload.items() if key != "attachments"}
            return [AttachmentContext.from_mapping({**base, "organization_id": base.get("organization_id") or organization_id, **item}) for item in raw_attachments if isinstance(item, dict)]
        if isinstance(payload.get("attachment"), dict):
            base = {key: value for key, value in payload.items() if key not in {"attachment", "attachments"}}
            return [AttachmentContext.from_mapping({**base, "organization_id": base.get("organization_id") or organization_id, **payload["attachment"]})]
        if payload.get("attachment_id") or payload.get("file_name") or payload.get("object_ref"):
            return [AttachmentContext.from_mapping({**payload, "organization_id": payload.get("organization_id") or organization_id})]
        knowledge_item_id = str(payload.get("knowledge_item_id") or "")
        if not knowledge_item_id:
            return []
        knowledge = self.knowledge.get_knowledge(knowledge_item_id, content_version=payload.get("content_version"), acl_version=payload.get("acl_version"))
        attachments = knowledge.get("attachments") if isinstance(knowledge, dict) else None
        if isinstance(attachments, list) and attachments:
            base = {key: value for key, value in knowledge.items() if key != "attachments"}
            base.setdefault("organization_id", organization_id)
            return [AttachmentContext.from_mapping({**base, **item, "knowledge_item_id": knowledge_item_id, "content_version": payload.get("content_version", knowledge.get("content_version", 1)), "acl_version": payload.get("acl_version", knowledge.get("acl_version", 0))}) for item in attachments if isinstance(item, dict)]
        content = self.knowledge.get_content(knowledge_item_id, content_version=payload.get("content_version"), acl_version=payload.get("acl_version"), content_variant=payload.get("content_variant") or "display")
        text = content.get("text") or content.get("content") or content.get("body") if isinstance(content, dict) else None
        if not isinstance(text, str) or not text.strip():
            return []
        temp = Path(tempfile.mkdtemp(prefix="rag-inline-")) / f"{knowledge_item_id}.txt"
        temp.write_text(text, encoding="utf-8")
        self._inline_temp_dirs.append(temp.parent)
        variant = str(payload.get("content_variant") or content.get("content_variant") or "display")
        part_kind = "knowledge_original" if variant == "original" else "message_display"
        return [AttachmentContext(attachment_id=f"message-{knowledge_item_id}", knowledge_item_id=knowledge_item_id, file_name=temp.name, mime_type="text/plain", file_path=str(temp), content_version=int(payload.get("content_version") or 1), acl_version=int(payload.get("acl_version") or 0), content_access_required=part_kind == "knowledge_original", organization_id=str(payload.get("organization_id") or organization_id) if (payload.get("organization_id") or organization_id) else None, part_kind=part_kind)]

    def _cleanup_inline_files(self) -> None:
        for directory in self._inline_temp_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        self._inline_temp_dirs.clear()

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
        self.repository.add_outbox_event(envelope, aggregate_type="knowledge_item", aggregate_id=str(payload.get("knowledge_item_id") or uuid.uuid4()))
        self.flush_outbox()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
