from __future__ import annotations

from app.application.entity_service import EntityMatcher, discover_candidate_names
from app.domain.rag import Chunk, normalized_text
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository, PostgresRagMVPRepository
from app.config import settings


class MemoryCandidateService:
    """MVP Memory Lane: candidate discovery only, never official tree mutation."""

    def __init__(self, repository: object | None = None) -> None:
        self.repository = repository or (
            PostgresRagMVPRepository() if settings.database_url else InMemoryRagMVPRepository()
        )

    def process(self, job: dict) -> dict[str, int]:
        chunks = self.repository.list_chunks(embedding_status="ready")
        chunks = [
            chunk for chunk in chunks
            if chunk.knowledge_item_id == job["knowledge_item_id"]
            and chunk.content_version == int(job["content_version"])
            and chunk.content_variant == "display"
            and chunk.lifecycle_status == "active"
        ]
        entities, aliases, version = self.repository.load_entity_registry(
            scope_type=job["scope_type"],
            scope_id=job["scope_id"],
        )
        matcher = EntityMatcher(entities, aliases, registry_version=version)
        candidates = 0
        mentions = 0
        for chunk in chunks:
            matched_ids = {match.entity_id for match in matcher.match_text(chunk.content)}
            if matched_ids:
                continue
            for name, domain in discover_candidate_names(chunk.content):
                key = normalized_text(name)
                if not key or any(
                    key in normalized_text(entity.canonical_name)
                    or normalized_text(entity.canonical_name) in key
                    for entity in entities
                ):
                    continue
                self.repository.upsert_candidate_mention(
                    scope_type=chunk.scope_type,
                    scope_id=chunk.scope_id,
                    candidate_name=name,
                    normalized_key=key,
                    domain=domain,
                    chunk=chunk,
                    context_excerpt=_excerpt(chunk.content, name),
                    confidence=0.55,
                    method="regex",
                )
                candidates += 1
                mentions += 1
        return {"candidate_count": candidates, "mention_count": mentions}


def _excerpt(content: str, name: str) -> str:
    index = content.find(name)
    if index < 0:
        return content[:240]
    start = max(0, index - 80)
    return content[start:index + len(name) + 120]
