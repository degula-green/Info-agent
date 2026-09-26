from __future__ import annotations

import logging
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
            chunk for chunk in self.repository.list_chunks(embedding_status="pending")
            if chunk.knowledge_item_id == job["knowledge_item_id"]
            and chunk.content_version == int(job["content_version"])
            and chunk.lifecycle_status == "active"
        ]
        if not chunks:
            return {"status": "metadata_only", "chunk_count": 0, "branch_count": 0}
        eligible = [chunk for chunk in chunks if chunk.rag_eligible and chunk.content.strip()]
        if eligible:
            try:
                vectors = self.embedding.embed([chunk.content for chunk in eligible])
            except Exception as exc:
                raise IndexStageError("EMBEDDING_FAILED", "embedding provider failed", retryable=True) from exc
            if len(vectors) != len(eligible):
                raise IndexStageError("EMBEDDING_COUNT_MISMATCH", "embedding provider returned wrong count")
            for chunk, vector in zip(eligible, vectors):
                if len(vector) != settings.embedding_dims:
                    raise IndexStageError("EMBEDDING_DIMENSION_MISMATCH", "embedding dimension mismatch")
                chunk.embedding = vector
                chunk.embedding_model = self.embedding.model
                chunk.embedding_dimensions = self.embedding.dimensions
                chunk.embedding_status = "ready"
        entities, aliases, registry_version = self.repository.load_entity_registry(
            scope_type=job["scope_type"],
            scope_id=job["scope_id"],
        )
        matcher = EntityMatcher(entities, aliases, registry_version=registry_version)
        branch_count = 0
        for chunk in chunks:
            branches = match_chunk_branches(chunk, matcher)
            chunk.branch_keys = tuple(branch.branch_key for branch in branches)
            chunk.registry_version = registry_version
            if branches:
                self.repository.replace_chunk_branches(chunk, branches)
                branch_count += len(branches)
            chunk.embedding_model = chunk.embedding_model or self.embedding.model
            chunk.embedding_dimensions = chunk.embedding_dimensions or self.embedding.dimensions
            chunk.embedding_status = "ready"
        self.indexer.delete_older_versions(
            resource_id=job["resource_id"],
            content_version=int(job["content_version"]),
        )
        try:
            self.indexer.index_chunks(chunks)
        except Exception as exc:
            self.repository.update_embedding_status(
                [chunk.chunk_id for chunk in chunks],
                status="failed",
                model=self.embedding.model,
                dimensions=self.embedding.dimensions,
            )
            raise IndexStageError("ES_INDEX_FAILED", "Elasticsearch indexing failed") from exc
        self.repository.update_embedding_status(
            [chunk.chunk_id for chunk in chunks],
            status="ready",
            model=self.embedding.model,
            dimensions=self.embedding.dimensions,
        )
        for chunk in chunks:
            alias = (
                settings.elasticsearch_protected_read_index
                if chunk.protected
                else settings.elasticsearch_display_read_index
            )
            self.repository.upsert_projection(
                chunk,
                alias=alias,
                mapping_version="v1",
                status="ready",
            )
        return {
            "status": "ready",
            "chunk_count": len(chunks),
            "vectorized_chunk_count": len(eligible),
            "branch_count": branch_count,
            "registry_version": registry_version,
        }
