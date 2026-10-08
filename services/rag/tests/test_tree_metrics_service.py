"""Zero-annotation tree metrics and their guard-rail alerts."""

from app.application.tree_metrics_service import TreeMetricsService, evaluate_alerts
from app.domain.rag import Chunk, EntityMount, normalized_text
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository

SCOPE = dict(scope_type="organization", scope_id="org-1")


def _chunk(index: int) -> Chunk:
    return Chunk(
        chunk_id=f"{index:064d}",
        resource_snapshot_id="00000000-0000-0000-0000-000000000001",
        knowledge_item_id="00000000-0000-0000-0000-000000000002",
        resource_type="message",
        resource_id=f"00000000-0000-0000-0000-{index:012d}",
        knowledge_base_id="00000000-0000-0000-0000-000000000003",
        scope_type=SCOPE["scope_type"],
        scope_id=SCOPE["scope_id"],
        content_version=1,
        processing_version="v1",
        chunking_version="v1",
        content_variant="display",
        chunk_index=index,
        chunk_count=1,
        content=f"消息 {index}",
        content_hash="0" * 64,
        sent_at=f"2026-10-01T00:{index:02d}:00Z",
        source_conversation_id="conv-1",
    )


def test_empty_scope_reports_zero_coverage_without_misleading_alerts():
    repo = InMemoryRagMVPRepository()

    snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

    assert snapshot["mount_coverage"] == 0.0
    assert snapshot["entity_count"] == 0
    # No entities means "no mounts" is expected, not a fault: the tree has not
    # been populated yet.
    assert snapshot["alerts"] == []


def test_coverage_counts_distinct_mounted_messages():
    repo = InMemoryRagMVPRepository()
    chunks = [_chunk(i) for i in range(1, 5)]
    repo.upsert_chunks(chunks)
    entity_id = repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )["id"]
    repo.merge_chunk_mounts(chunks[0], [EntityMount(entity_id, "project", 1, "window_batch", 0.85)])
    repo.merge_chunk_mounts(chunks[1], [EntityMount(entity_id, "project", 1, "window_batch", 0.85)])

    snapshot = TreeMetricsService(repository=repo).snapshot(**SCOPE)

    assert snapshot["message_count"] == 4
    assert snapshot["mounted_chunk_count"] == 2
    assert snapshot["mount_count"] == 2
    assert snapshot["mount_coverage"] == 0.5
    assert snapshot["mount_methods"]["window_batch"]["count"] == 2
    assert snapshot["mount_methods"]["window_batch"]["avg_confidence"] == 0.85


def test_entities_without_mounts_raise_the_no_mounts_alert():
    repo = InMemoryRagMVPRepository()
    repo.upsert_chunks([_chunk(1)])
    repo.upsert_entity(
        domain="project", canonical_name="A项目", normalized_key=normalized_text("A项目"), **SCOPE
    )
    repo.update_entity_embedding(
        entity_id=repo.entities[0]["id"], embedding=[1.0], model="fake", dimensions=1
    )

    alerts = TreeMetricsService(repository=repo).snapshot(**SCOPE)["alerts"]

    assert "no_mounts" in alerts


def test_candidate_backlog_and_missing_embeddings_are_flagged():
    alerts = evaluate_alerts({
        "message_count": 10,
        "entity_count": 10,
        "mount_count": 5,
        "relation_count": 1,
        "pending_candidate_count": 150,
        "entities_missing_embedding": 5,
    })

    assert "candidate_backlog" in alerts
    # Half the entities have no vector, so L3 cannot see them.
    assert "entities_missing_embedding" in alerts
    assert "no_mounts" not in alerts


def test_missing_relations_is_reported_separately():
    alerts = evaluate_alerts({
        "message_count": 10,
        "entity_count": 2,
        "mount_count": 4,
        "relation_count": 0,
        "pending_candidate_count": 0,
        "entities_missing_embedding": 0,
    })

    assert alerts == ["no_relations"]
