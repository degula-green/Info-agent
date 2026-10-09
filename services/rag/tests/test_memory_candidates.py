"""Candidate discovery source: the window scan owns it since Phase 2."""

from app.application.memory_service import MemoryCandidateService
from app.config import settings
from app.domain.rag import Chunk
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")
ITEM = "00000000-0000-0000-0000-000000000002"
SCOPED_CONTENT = "云启项目今天上线，另外签了采购合同"


def _repository_with_content(content: str) -> InMemoryRagMVPRepository:
    repository = InMemoryRagMVPRepository()
    repository.upsert_chunks([
        Chunk(
            chunk_id="0" * 64,
            resource_snapshot_id="00000000-0000-0000-0000-000000000001",
            knowledge_item_id=ITEM,
            resource_type="message",
            resource_id="00000000-0000-0000-0000-000000000009",
            knowledge_base_id="00000000-0000-0000-0000-000000000003",
            scope_type=SCOPE["scope_type"],
            scope_id=SCOPE["scope_id"],
            content_version=1,
            processing_version="v1",
            chunking_version="v1",
            content_variant="display",
            chunk_index=0,
            chunk_count=1,
            content=content,
            content_hash="0" * 64,
            sent_at="2026-10-01T00:00:00Z",
            embedding_status="ready",
        )
    ])
    return repository


def _job() -> dict:
    return {"knowledge_item_id": ITEM, "content_version": 1, **SCOPE}


def _run_with_regex(enabled: bool, repository):
    service = MemoryCandidateService(repository=repository)
    original = settings.memory_regex_candidates_enabled
    object.__setattr__(settings, "memory_regex_candidates_enabled", enabled)
    try:
        return service.process(_job())
    finally:
        object.__setattr__(settings, "memory_regex_candidates_enabled", original)


def test_regex_candidates_are_off_by_default():
    # Content that the old patterns would happily mine.
    repository = _repository_with_content(SCOPED_CONTENT)

    result = _run_with_regex(False, repository)

    assert result["candidate_count"] == 0
    assert repository.candidates == {}


def test_the_transitional_regex_source_still_works_when_switched_on():
    repository = _repository_with_content(SCOPED_CONTENT)

    result = _run_with_regex(True, repository)

    assert result["candidate_count"] >= 1
    names = {row["candidate_name"] for row in repository.candidates.values()}
    assert "云启项目" in names


def test_the_regex_pattern_over_captures_which_is_why_it_is_off():
    # Not a bug report - a record of the behaviour that made the switch worth
    # doing: a 2-40 character prefix before 合同 pulls the whole clause in.
    repository = _repository_with_content(SCOPED_CONTENT)

    _run_with_regex(True, repository)

    names = {row["candidate_name"] for row in repository.candidates.values()}
    assert any(len(name) > len("云启项目") for name in names), names
