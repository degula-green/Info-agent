from __future__ import annotations

from typing import Any, Protocol

from app.domain.rag import (
    AccessCheck,
    AuthorizationScope,
    Chunk,
    Entity,
    EntityAlias,
    EntityMount,
    ResourceContext,
    SearchRequest,
    SearchResult,
)


class TaskRepository(Protocol):
    def create_or_get_job(self, envelope: dict[str, Any], *, processing_version: str | None = None) -> dict[str, Any]: ...
    def get_job(self, job_id: str | None = None, *, source_event_id: str | None = None) -> dict[str, Any] | None: ...
    def claim_jobs(
        self,
        lane: str,
        *,
        limit: int = 1,
        lease_seconds: int | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]: ...
    def list_recoverable_jobs(
        self,
        lane: str,
        *,
        limit: int = 1,
    ) -> list[dict[str, Any]]: ...
    def heartbeat(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        lease_seconds: int | None = None,
    ) -> bool: ...
    def update_job(self, job_id: str, **fields: Any) -> None: ...
    def update_job_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool: ...
    def complete_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool: ...
    def fail_if_owned(
        self,
        job_id: str,
        *,
        owner: str,
        epoch: int,
        fields: dict[str, Any],
    ) -> bool: ...
    def add_attempt(
        self,
        job_id: str,
        *,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
        **fields: Any,
    ) -> str: ...


class SourceSnapshotRepository(Protocol):
    def upsert_snapshot(self, context: ResourceContext) -> str: ...


class ChunkRepository(Protocol):
    def upsert_chunks(self, chunks: list[Chunk]) -> int: ...
    def list_chunks(self, **filters: Any) -> list[Chunk]: ...
    def update_embedding_status(self, chunk_ids: list[str], **values: Any) -> None: ...
    def mark_older_chunks_inactive(self, *, knowledge_item_id: str, content_version: int) -> None: ...
    def delete_resource_data(self, *, knowledge_item_id: str, resource_id: str) -> int: ...


class ProjectionRepository(Protocol):
    def upsert_projection(self, chunk: Chunk, **values: Any) -> None: ...
    def ensure_projection_records(
        self,
        chunks: list[Chunk],
        *,
        mapping_version: str,
    ) -> dict[str, dict[str, Any]]: ...
    def list_projection_records(
        self,
        *,
        knowledge_item_id: str,
        content_version: int,
    ) -> list[dict[str, Any]]: ...
    def update_projection_status(
        self,
        chunk_ids: list[str],
        *,
        status: str,
        failure_stage: str | None = None,
        error: str | None = None,
        increment_retry: bool = False,
        next_retry_at: Any | None = None,
    ) -> int: ...


class OutboxRepository(Protocol):
    def add_outbox_event(self, event: dict[str, Any]) -> str: ...
    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]: ...
    def mark_outbox_published(self, event_id: str) -> None: ...
    def mark_outbox_failed(self, event_id: str, error: str) -> None: ...


class EntityRegistryRepository(Protocol):
    def load_entity_registry(self, *, scope_type: str, scope_id: str) -> tuple[list[Entity], list[EntityAlias], int]: ...
    def locate_entities_exact(self, *, scope_type: str, scope_id: str, normalized: str) -> list[dict[str, Any]]: ...
    def locate_entities_fuzzy(
        self, *, scope_type: str, scope_id: str, normalized: str, limit: int = 10
    ) -> list[dict[str, Any]]: ...
    def locate_entities_semantic(
        self, *, scope_type: str, scope_id: str, embedding: list[float], limit: int = 10
    ) -> list[dict[str, Any]]: ...
    def list_entities_pending_embedding(self, *, limit: int = 50) -> list[dict[str, Any]]: ...
    def list_scan_conversations(self, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def get_scan_watermark(
        self, *, scope_type: str, scope_id: str, conversation_id: str
    ) -> str | None: ...
    def list_conversation_chunks(
        self,
        *,
        scope_type: str,
        scope_id: str,
        conversation_id: str,
        after_sent_at: str | None = None,
        limit: int = 500,
    ) -> list[Chunk]: ...
    def set_scan_watermark(
        self,
        *,
        scope_type: str,
        scope_id: str,
        conversation_id: str,
        last_sent_at: str,
        last_chunk_id: str | None = None,
        window_count: int = 0,
    ) -> None: ...
    def find_entities_by_normalized(
        self, *, scope_type: str, scope_id: str, normalized_keys: list[str]
    ) -> dict[str, dict[str, Any]]: ...
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
    ) -> None: ...
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
    ) -> list[dict[str, Any]]: ...
    def upsert_eval_case(
        self,
        *,
        scope_type: str,
        scope_id: str,
        suite: str,
        dataset_version: int,
        query: str,
        labels: dict[str, Any],
        notes: str | None = None,
        created_by: str | None = None,
    ) -> str: ...
    def list_eval_cases(
        self,
        *,
        scope_type: str,
        scope_id: str,
        suite: str,
        dataset_version: int | None = None,
        status: str | None = "active",
    ) -> list[dict[str, Any]]: ...
    def record_eval_run(
        self,
        *,
        scope_type: str,
        scope_id: str,
        suite: str,
        dataset_version: int,
        case_count: int,
        passed_count: int,
        metrics: dict[str, Any],
        label: str | None = None,
    ) -> str: ...
    def list_eval_runs(
        self,
        *,
        scope_type: str,
        scope_id: str,
        suite: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...
    def update_entity_embedding(
        self,
        *,
        entity_id: str,
        embedding: list[float],
        model: str,
        dimensions: int,
        status: str = "ready",
    ) -> None: ...
    def upsert_candidate_mention(self, **values: Any) -> str: ...
    def list_candidates(self, **filters: Any) -> tuple[list[dict[str, Any]], int]: ...
    def get_candidate(self, **filters: Any) -> dict[str, Any] | None: ...
    def review_candidate(self, **values: Any) -> dict[str, Any]: ...


class BranchRepository(Protocol):
    def replace_chunk_mounts(self, chunk: Chunk, mounts: list[EntityMount]) -> None: ...
    def merge_chunk_mounts(self, chunk: Chunk, mounts: list[EntityMount]) -> None: ...
    def get_tree(self, *, scope_type: str, scope_id: str) -> dict[str, Any]: ...
    def tree_metrics(self, *, scope_type: str, scope_id: str) -> dict[str, Any]: ...


class SearchHistoryRepository(Protocol):
    def record_search(self, **values: Any) -> None: ...


class QAHistoryRepository(Protocol):
    def create_qa_conversation(self, **values: Any) -> str: ...
    def add_qa_message(self, **values: Any) -> str: ...
    def update_qa_message(self, message_id: str, **values: Any) -> bool: ...
    def list_qa_conversations(self, *, user_id: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]: ...
    def get_qa_conversation(self, *, user_id: str, conversation_id: str) -> dict[str, Any] | None: ...
    def rename_qa_conversation(self, *, user_id: str, conversation_id: str, title: str) -> bool: ...
    def delete_qa_conversation(self, *, user_id: str, conversation_id: str) -> bool: ...


class KnowledgeSource(Protocol):
    def get_knowledge(
        self,
        knowledge_item_id: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]: ...
    def get_content(
        self,
        knowledge_item_id: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        content_variant: str | None = None,
        purpose: str | None = None,
        rag_job_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]: ...
    def get_attachment(
        self,
        attachment_id: str,
        *,
        content_version: int | None = None,
        acl_version: int | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]: ...


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SearchIndexer(Protocol):
    def create_indices(self, *, recreate: bool = False) -> list[str]: ...
    def index_chunks(self, chunks: list[Chunk]) -> int: ...
    def delete_older_versions(self, *, resource_id: str, content_version: int) -> int: ...
    def search_bm25(
        self,
        request: SearchRequest,
        *,
        entity_ids: tuple[str, ...] = (),
        protected_object_keys: tuple[str, ...] = (),
        size: int | None = None,
    ) -> list[SearchResult]: ...
    def search_knn(
        self,
        request: SearchRequest,
        query_vector: list[float],
        *,
        entity_ids: tuple[str, ...] = (),
        protected_object_keys: tuple[str, ...] = (),
        size: int | None = None,
    ) -> list[SearchResult]: ...
    def search_neighbors(
        self,
        request: SearchRequest,
        anchors: list[SearchResult],
        *,
        protected_object_keys: tuple[str, ...] = (),
        radius: int = 1,
    ) -> list[SearchResult]: ...


class AuthorizationGateway(Protocol):
    def search_scope(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        resource_parts: tuple[str, ...],
    ) -> AuthorizationScope: ...
    def check_batch(
        self,
        *,
        user_id: str,
        scope_type: str,
        scope_id: str,
        checks: list[AccessCheck],
        snapshot_id: str | None = None,
    ) -> list[bool]: ...


class CallbackPublisher(Protocol):
    def send(self, payload: dict[str, Any]) -> None: ...


class AnswerProvider(Protocol):
    def generate(self, question: str, results: list[SearchResult]) -> str: ...
    def generate_stream(
        self,
        question: str,
        results: list[SearchResult],
    ) -> Any: ...
