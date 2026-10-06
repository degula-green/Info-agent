-- Rebuild ACLs for organization knowledge items after the organization
-- relation was added to the OpenFGA resource model. Existing rows may still
-- be marked as synced even though their resource tuple was created by the
-- previous relation set.
UPDATE knowledge.knowledge_items
SET permission_ready = FALSE,
    acl_sync_status = 'pending',
    last_error = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE knowledge_scope = 'organization'
  AND lifecycle_status = 'active';

INSERT INTO knowledge.outbox_events (
    id,
    aggregate_type,
    aggregate_id,
    event_type,
    event_version,
    schema_version,
    organization_id,
    trace_id,
    payload,
    status,
    retry_count,
    available_at
)
SELECT
    gen_random_uuid(),
    'knowledge_item',
    ki.id,
    'permission.sync.requested',
    ki.content_version,
    1,
    ki.organization_id,
    'migration:knowledge-permission-acl-backfill',
    jsonb_build_object(
        'knowledge_item_id', ki.id::text,
        'content_version', ki.content_version,
        'reason', 'organization-resource-relation-backfill'
    ),
    'pending',
    0,
    CURRENT_TIMESTAMP
FROM knowledge.knowledge_items ki
WHERE ki.knowledge_scope = 'organization'
  AND ki.lifecycle_status = 'active'
ON CONFLICT (aggregate_type, aggregate_id, event_type, event_version)
DO UPDATE SET
    status = 'pending',
    retry_count = 0,
    last_error = NULL,
    available_at = CURRENT_TIMESTAMP,
    published_at = NULL;
