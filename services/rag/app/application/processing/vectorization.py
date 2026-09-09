from __future__ import annotations

import math

from app.application.ports import EmbeddingProvider
from app.config import settings
from app.domain.models import ChunkRecord


class VectorizationError(RuntimeError):
    pass


def vectorize_chunks(chunks: list[ChunkRecord], provider: EmbeddingProvider) -> list[ChunkRecord]:
    eligible = [chunk for chunk in chunks if chunk.rag_eligible and chunk.content.strip()]
    if not eligible:
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
    return chunks
