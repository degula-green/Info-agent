"""Review dwell time: optional, clamped, and never confused with zero."""

from app.application.entity_review_service import (
    MAX_REVIEW_DURATION_MS,
    EntityReviewService,
    _review_duration_ms,
)
from app.domain.rag import Chunk
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository


def _chunk():
    return Chunk(
        chunk_id="0" * 64,
        resource_snapshot_id="00000000-0000-0000-0000-000000000001",
        knowledge_item_id="00000000-0000-0000-0000-000000000002",
        resource_type="message",
        resource_id="00000000-0000-0000-0000-000000000009",
        knowledge_base_id="00000000-0000-0000-0000-000000000003",
        scope_type="organization",
        scope_id="org-1",
        content_version=1,
        processing_version="v1",
        chunking_version="v1",
        content_variant="display",
        chunk_index=0,
        chunk_count=1,
        content="A项目的服务器配置",
        content_hash="0" * 64,
        sent_at="2026-10-01T00:00:00Z",
    )


def test_missing_dwell_time_stays_null():
    # A scripted or API-driven review has no dwell time. That is different from
    # "took zero milliseconds", and the average must not treat it as zero.
    assert _review_duration_ms(None) is None


def test_dwell_time_is_clamped_to_one_day():
    assert _review_duration_ms(-5) == 0
    assert _review_duration_ms(4000) == 4000
    assert _review_duration_ms(MAX_REVIEW_DURATION_MS + 1) == MAX_REVIEW_DURATION_MS


def test_unparsable_dwell_time_is_dropped_rather_than_raising():
    # The service is reached by the router (already validated), but also by
    # scripts; a bad value must not fail the whole review.
    assert _review_duration_ms("not-a-number") is None


def test_the_service_passes_a_clamped_value_to_the_repository():
    repository = InMemoryRagMVPRepository()
    service = EntityReviewService(repository=repository)
    chunk = _chunk()
    repository.upsert_chunks([chunk])
    candidate_id = repository.upsert_candidate_mention(
        scope_type="organization", scope_id="org-1",
        candidate_name="A项目", normalized_key="a项目", domain="project",
        chunk=chunk, context_excerpt=chunk.content, confidence=0.85, method="llm",
    )

    service.review(
        scope_type="organization", scope_id="org-1",
        candidate_id=candidate_id, reviewer_id="00000000-0000-0000-0000-000000000099",
        review_request_id="00000000-0000-0000-0000-000000000098",
        idempotency_key=None, action="ignore", expected_status="new",
        canonical_name=None, domain=None, target_entity_id=None, note=None,
        duration_ms=MAX_REVIEW_DURATION_MS * 10,
    )

    stored = repository.reviews[-1]["duration_ms"]
    assert stored == MAX_REVIEW_DURATION_MS
