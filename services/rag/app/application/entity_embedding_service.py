"""Backfill entity embeddings.

The semantic location layer (L3) can only match entities that carry a vector,
and entity rows are created without one: embedding is an external call, so
publishing an entity must not block on it. Rows are therefore created
``embedding_status='pending'`` and filled in by this service.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable


logger = logging.getLogger("rag.entity-embedding")


class EntityEmbeddingService:
    def __init__(self, *, repository: Any, embedding: Any, batch_size: int = 50) -> None:
        self.repository = repository
        self.embedding = embedding
        self.batch_size = max(1, int(batch_size))

    def run_once(self, *, limit: int | None = None) -> int:
        """Embed and store one batch. Returns how many entities were updated.

        A failure is contained: the rows stay ``pending`` and the next sweep
        retries, so a flaky embedding provider never fails a review action.
        """
        pending = self.repository.list_entities_pending_embedding(
            limit=limit or self.batch_size
        )
        if not pending:
            return 0
        texts = [entity_embedding_text(item) for item in pending]
        try:
            vectors = self.embedding.embed(texts)
        except Exception:
            logger.exception("entity embedding batch failed")
            return 0
        if len(vectors) != len(pending):
            logger.warning(
                "entity embedding count mismatch: asked=%d got=%d",
                len(pending), len(vectors),
            )
            return 0

        updated = 0
        for item, vector in zip(pending, vectors):
            if not vector:
                continue
            self.repository.update_entity_embedding(
                entity_id=item["entity_id"],
                embedding=list(vector),
                model=str(getattr(self.embedding, "model", "") or "unknown"),
                dimensions=int(getattr(self.embedding, "dimensions", len(vector)) or len(vector)),
            )
            updated += 1
        return updated


def entity_embedding_text(item: dict[str, Any]) -> str:
    """Text an entity is embedded from.

    Aliases belong here: a user typing "aims" has to land near "AIMS系统开发项目"
    in vector space, and the alias is the only place that surface form lives.
    """
    parts: Iterable[str] = (
        str(item.get("canonical_name") or ""),
        str(item.get("description") or ""),
        str(item.get("keywords") or ""),
        str(item.get("aliases") or ""),
    )
    return " ".join(part.strip() for part in parts if part and part.strip())
