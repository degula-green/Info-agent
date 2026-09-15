from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from app.domain.models import (
    AccessCheck,
    AttachmentContext,
    AuthorizationScope,
    CanonicalBlock,
    ChunkRecord,
    ParsedDocument,
    SearchRequest,
    SearchResult,
)


class AuthorizationGateway(Protocol):
    def search_scope(
        self,
        *,
        user_id: str,
        organization_id: str | None,
        resource_parts: tuple[str, ...],
        knowledge_base_id: str | None = None,
    ) -> AuthorizationScope:
        ...

    def check_batch(
        self,
        *,
        user_id: str,
        organization_id: str | None,
        checks: list[AccessCheck],
        snapshot_id: str | None = None,
    ) -> list[bool]:
        ...


class KnowledgeSource(Protocol):
    def get_knowledge(self, knowledge_item_id: str, *, content_version: int | None = None, acl_version: int | None = None) -> dict[str, Any]:
        ...

    def get_content(self, knowledge_item_id: str, *, content_version: int | None = None, acl_version: int | None = None, content_variant: str | None = None) -> dict[str, Any]:
        ...

    def get_attachment(self, attachment_id: str, *, content_version: int | None = None, acl_version: int | None = None) -> dict[str, Any]:
        ...


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class ChunkIndexer(Protocol):
    def index_chunks(self, chunks: list[ChunkRecord]) -> int:
        ...

    def delete_version(self, *, knowledge_item_id: str, content_version: int) -> int:
        ...

    def delete_older_versions(self, *, knowledge_item_id: str, content_version: int) -> int:
        ...

    def delete_older_versions(self, *, knowledge_item_id: str, content_version: int) -> int:
        ...


class EventPublisher(Protocol):
    def publish(self, envelope: dict[str, Any]) -> str:
        ...


class RagStateRepository(Protocol):
    def get_processing_job(self, source_event_id: str) -> dict[str, Any] | None:
        ...

    def create_processing_job(self, envelope: dict[str, Any], *, job_type: str) -> str | None:
        ...

    def update_processing_job(self, job_id: str, **fields: Any) -> None:
        ...

    def upsert_index_record(self, *, knowledge_item_id: str, organization_id: str | None, content_version: int, acl_version: int, content_variant: str, es_index_alias: str, es_document_prefix: str, chunk_count: int, mapping_version: str, status: str) -> None:
        ...

    def record_search(self, *, user_id: str, organization_id: str | None, query_text: str, query_hash: str, filters: dict[str, Any], result_count: int, duration_ms: int, request_id: str | None) -> None:
        ...

    def add_outbox_event(self, envelope: dict[str, Any], *, aggregate_type: str, aggregate_id: str, event_version: int = 1) -> str:
        ...

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        ...

    def mark_outbox_published(self, event_id: str) -> None:
        ...

    def create_qa_conversation(self, *, user_id: str, organization_id: str | None, title: str | None = None) -> str:
        ...

    def add_qa_message(self, *, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]] | None = None, model_name: str | None = None, prompt_version: str | None = None, status: str = "completed") -> str:
        ...


class DocumentParser(Protocol):
    def parse(self, context: AttachmentContext) -> ParsedDocument:
        ...


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        ...

    def put_json(self, key: str, value: dict[str, Any]) -> str:
        ...

    def put_text(self, key: str, value: str, content_type: str = "text/plain; charset=utf-8") -> str:
        ...


@dataclass(frozen=True)
class ProcessingInput:
    attachment: AttachmentContext
    knowledge_item: dict[str, Any] | None = None
    trace_id: str | None = None
    processing_job_id: str | None = None


@dataclass
class ProcessingOutput:
    attachment: AttachmentContext
    parsed: ParsedDocument
    chunks: list[ChunkRecord]
    indexed_count: int = 0
    status: str = "succeeded"
    error_code: str | None = None
    error_message: str | None = None


class SearchEngine(Protocol):
    def search(self, request: SearchRequest) -> list[SearchResult]:
        ...


class AnswerProvider(Protocol):
    def generate(self, question: str, results: list[SearchResult]) -> str:
        ...

    def generate_stream(self, question: str, results: list[SearchResult]):
        ...
