from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.application.entity_service import EntityMatcher, match_chunk_branches
from app.application.mvp_ports import EmbeddingProvider, SearchIndexer
from app.config import settings
from app.domain.rag import Chunk
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository, PostgresRagMVPRepository


logger = logging.getLogger("rag.index")


class IndexStageError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MVPIndexService:
    def __init__(
        self,
        *,
        repository: object | None = None,
        indexer: SearchIndexer,
        embedding: EmbeddingProvider,
    ) -> None:
        self.repository = repository or (
            PostgresRagMVPRepository() if settings.database_url else InMemoryRagMVPRepository()
        )
        self.indexer = indexer
        self.embedding = embedding

    def process(self, job: dict[str, Any]) -> dict[str, Any]:
        chunks = [
            chunk for chunk in self.repository.list_chunks()
            if chunk.knowledge_item_id == job["knowledge_item_id"]
            and chunk.content_version == int(job["content_version"])
            and chunk.lifecycle_status == "active"
        ]
        if not chunks:
            if job.get("parse_status") == "metadata_only":
                return {
                    "status": "metadata_only",
                    "chunk_count": 0,
                    "branch_count": 0,
                }
            raise IndexStageError(
                "PARSED_CONTENT_WITHOUT_CHUNKS",
                "parsed content did not produce chunks",
                retryable=False,
            )

        projections = self.repository.ensure_projection_records(
            chunks,
            mapping_version="v1",
        )
        retryable_chunks = self._retryable_chunks(chunks, projections)
        embedding_chunks = [
            chunk
            for chunk in retryable_chunks
            if chunk.rag_eligible
            and chunk.content.strip()
            and chunk.embedding_status != "ready"
        ]
        if embedding_chunks:
            try:
                vectors = self.embedding.embed(
                    [chunk.content for chunk in embedding_chunks]
                )
            except Exception as exc:
                retryable = self._mark_retryable_failure(
                    embedding_chunks,
                    failure_stage="embedding",
                    error=str(exc),
                )
                raise IndexStageError(
                    "EMBEDDING_FAILED",
                    "embedding provider failed",
                    retryable=retryable,
                ) from exc
            if len(vectors) != len(embedding_chunks):
                retryable = self._mark_retryable_failure(
                    embedding_chunks,
                    failure_stage="embedding",
                    error="embedding provider returned wrong count",
                )
                raise IndexStageError(
                    "EMBEDDING_COUNT_MISMATCH",
                    "embedding provider returned wrong count",
                    retryable=retryable,
                )
            for chunk, vector in zip(embedding_chunks, vectors):
                if len(vector) != settings.embedding_dims:
                    retryable = self._mark_retryable_failure(
                        [chunk],
                        failure_stage="embedding",
                        error="embedding dimension mismatch",
                    )
                    raise IndexStageError(
                        "EMBEDDING_DIMENSION_MISMATCH",
                        "embedding dimension mismatch",
                        retryable=retryable,
                    )
                chunk.embedding = vector
                chunk.embedding_model = self.embedding.model
                chunk.embedding_dimensions = self.embedding.dimensions
                chunk.embedding_status = "ready"
            self.repository.update_embedding_status(
                [chunk.chunk_id for chunk in embedding_chunks],
                status="ready",
                model=self.embedding.model,
                dimensions=self.embedding.dimensions,
            )

        entities, aliases, registry_version = self.repository.load_entity_registry(
            scope_type=job["scope_type"],
            scope_id=job["scope_id"],
        )
        matcher = EntityMatcher(entities, aliases, registry_version=registry_version)
        branch_count = 0
        indexable: list[Chunk] = []
        for chunk in retryable_chunks:
            branches = match_chunk_branches(chunk, matcher)
            chunk.branch_keys = tuple(branch.branch_key for branch in branches)
            chunk.registry_version = registry_version
            if branches:
                self.repository.replace_chunk_branches(chunk, branches)
                branch_count += len(branches)
            chunk.embedding_model = chunk.embedding_model or self.embedding.model
            chunk.embedding_dimensions = chunk.embedding_dimensions or self.embedding.dimensions
            indexable.append(chunk)

        self.repository.update_projection_status(
            [chunk.chunk_id for chunk in indexable],
            status="indexing",
        )
        try:
            self.indexer.index_chunks(indexable)
        except Exception as exc:
            retryable = self._mark_retryable_failure(
                indexable,
                failure_stage="indexing",
                error=str(exc),
            )
            raise IndexStageError(
                "ES_INDEX_FAILED",
                "Elasticsearch indexing failed",
                retryable=retryable,
            ) from exc
        self.repository.update_projection_status(
            [chunk.chunk_id for chunk in indexable],
            status="ready",
        )
        refreshed = self.repository.list_projection_records(
            knowledge_item_id=job["knowledge_item_id"],
            content_version=int(job["content_version"]),
        )
        statuses = {item["status"] for item in refreshed}
        if "failed" in statuses:
            raise IndexStageError(
                "PROJECTION_FAILED",
                "one or more projections exhausted retries",
                retryable=False,
            )
        if statuses - {"ready"}:
            raise IndexStageError(
                "PROJECTION_RETRY_PENDING",
                "current version projections are not ready",
                retryable=True,
            )
        self.indexer.delete_older_versions(
            resource_id=job["resource_id"],
            content_version=int(job["content_version"]),
        )
        return {
            "status": "ready",
            "chunk_count": len(chunks),
            "vectorized_chunk_count": len(
                [chunk for chunk in chunks if chunk.rag_eligible and chunk.content.strip()]
            ),
            "branch_count": branch_count,
            "registry_version": registry_version,
        }

    def _retryable_chunks(
        self,
        chunks: list[Chunk],
        projections: dict[str, dict[str, Any]],
    ) -> list[Chunk]:
        now = datetime.now(timezone.utc)
        output: list[Chunk] = []
        for chunk in chunks:
            projection = projections.get(chunk.chunk_id) or {}
            status = projection.get("status")
            next_retry_at = projection.get("next_retry_at")
            if status == "ready":
                continue
            if status == "failed":
                raise IndexStageError(
                    "PROJECTION_FAILED",
                    f"projection already failed for chunk {chunk.chunk_id}",
                    retryable=False,
                )
            if status == "retry_wait" and next_retry_at and next_retry_at > now:
                continue
            output.append(chunk)
        return output

    def _mark_retryable_failure(
        self,
        chunks: list[Chunk],
        *,
        failure_stage: str,
        error: str,
    ) -> bool:
        max_retries = max(1, settings.index_max_retries)
        retryable_ids: list[str] = []
        terminal_ids: list[str] = []
        current = {
            item["chunk_id"]: item
            for item in self.repository.list_projection_records(
                knowledge_item_id=chunks[0].knowledge_item_id,
                content_version=chunks[0].content_version,
            )
        } if chunks else {}
        for chunk in chunks:
            retry_count = int(current.get(chunk.chunk_id, {}).get("retry_count") or 0)
            if retry_count + 1 >= max_retries:
                terminal_ids.append(chunk.chunk_id)
            else:
                retryable_ids.append(chunk.chunk_id)
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        if retryable_ids:
            self.repository.update_projection_status(
                retryable_ids,
                status="retry_wait",
                failure_stage=failure_stage,
                error=error,
                increment_retry=True,
                next_retry_at=retry_at,
            )
        if terminal_ids:
            self.repository.update_projection_status(
                terminal_ids,
                status="failed",
                failure_stage=failure_stage,
                error=error,
                increment_retry=True,
            )
        return bool(retryable_ids)
