from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.application.ports import EmbeddingProvider
from app.config import settings
from app.domain.models import ChunkRecord


logger = logging.getLogger("rag.vectorize")


class VectorizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VectorizationReport:
    """Why the pipeline did or did not embed each chunk.

    Returning the chunks alone made a skipped embedding indistinguishable from
    a successful one: a chunk that is never vectorized lands in the index with
    ``embedding_status='pending'`` and is invisible to kNN while BM25 still
    finds it, so recall degrades with no signal anywhere. These counts are that
    signal, and they are also what the job report carries back to Knowledge.
    """

    total: int = 0
    vectorized: int = 0
    skipped_ineligible: int = 0
    skipped_empty: int = 0
    omitted: int = 0

    @property
    def skipped(self) -> int:
        return self.total - self.vectorized

    def reasons(self) -> dict[str, int]:
        return {
            "ineligible": self.skipped_ineligible,
            "empty_content": self.skipped_empty,
            "embedding_not_attempted": self.omitted,
        }


def vectorization_report(chunks: list[ChunkRecord]) -> VectorizationReport:
    """Classify ``chunks`` by vectorization outcome without re-embedding anything."""
    vectorized = sum(1 for chunk in chunks if chunk.vectorized)
    ineligible = sum(1 for chunk in chunks if not chunk.rag_eligible)
    empty = sum(1 for chunk in chunks if chunk.rag_eligible and not chunk.content.strip())
    # Eligible, non-empty, and still without a vector: the only category that
    # represents real loss rather than an intentional skip.
    omitted = len(chunks) - vectorized - ineligible - empty
    return VectorizationReport(
        total=len(chunks),
        vectorized=vectorized,
        skipped_ineligible=ineligible,
        skipped_empty=empty,
        omitted=max(0, omitted),
    )


def vectorize_chunks(chunks: list[ChunkRecord], provider: EmbeddingProvider) -> list[ChunkRecord]:
    eligible = [chunk for chunk in chunks if chunk.rag_eligible and chunk.content.strip()]
    if not eligible:
        report = vectorization_report(chunks)
        if report.skipped:
            logger.warning(
                "no chunk was vectorized: total=%d ineligible=%d empty_content=%d",
                report.total, report.skipped_ineligible, report.skipped_empty,
            )
        return chunks
    if len({chunk.chunk_id for chunk in eligible}) != len(eligible):
        raise VectorizationError("chunk IDs must be unique before vectorization")
    batch_size = max(1, settings.embedding_batch_size)
    for start in range(0, len(eligible), batch_size):
        batch = eligible[start : start + batch_size]
        vectors = provider.embed([chunk.content for chunk in batch])
        if len(vectors) != len(batch):
            raise VectorizationError("embedding provider returned an unexpected number of vectors")
        for chunk, vector in zip(batch, vectors):
            if len(vector) != provider.dimensions or not all(math.isfinite(float(value)) for value in vector):
                raise VectorizationError("embedding vector dimensions or values are invalid")
            chunk.embedding = [float(value) for value in vector]
            chunk.embedding_model = provider.model
            chunk.vectorized = True
    report = vectorization_report(chunks)
    if report.skipped:
        logger.warning(
            "partially vectorized: total=%d vectorized=%d ineligible=%d empty_content=%d",
            report.total, report.vectorized, report.skipped_ineligible, report.skipped_empty,
        )
    return chunks
