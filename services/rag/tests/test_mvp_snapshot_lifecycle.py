"""The RAG snapshot lifecycle must stay inside resource_snapshots_lifecycle_chk.

The knowledge item's lifecycle carries processing states ("ready"), while the
snapshot column only models active / inactive / deleted. Copying "ready"
through violated the CHECK constraint and failed the whole indexing job, which
is why an archived local-library file could never be found by content search.
"""

from app.domain.rag import ResourceContext, _snapshot_lifecycle

ITEM_ID = "11111111-1111-1111-1111-111111111111"
RESOURCE_ID = "22222222-2222-2222-2222-222222222222"
BASE_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"


def test_processing_states_map_onto_the_snapshot_lifecycle():
    assert _snapshot_lifecycle("ready") == "active"
    assert _snapshot_lifecycle("processing") == "active"
    assert _snapshot_lifecycle("pending") == "active"
    assert _snapshot_lifecycle("active") == "active"
    assert _snapshot_lifecycle(None) == "active"
    assert _snapshot_lifecycle("inactive") == "inactive"
    assert _snapshot_lifecycle("deleted") == "deleted"


def test_a_ready_knowledge_item_becomes_an_active_snapshot():
    context = ResourceContext.from_event_and_source(
        {
            "scope_type": "user",
            "scope_id": USER_ID,
            "resource_type": "attachment",
            "knowledge_item_id": ITEM_ID,
            "resource_id": RESOURCE_ID,
            "knowledge_base_id": BASE_ID,
        },
        {
            "content_hash": "a" * 64,
            "lifecycle_status": "ready",
        },
    )

    assert context.lifecycle_status == "active"
