BEGIN;

-- Older installations created attachments from 000001 before the connector
-- contract added the ingestion-facing projection used by the Knowledge API.
-- Keep the migration additive so it is safe to apply to already aligned DBs.
ALTER TABLE knowledge.message_sources
    ADD COLUMN IF NOT EXISTS ingest_cursor TEXT;

CREATE TABLE IF NOT EXISTS knowledge.collector_cursor_receipts (
    collector_id UUID NOT NULL REFERENCES knowledge.conversation_collectors(id),
    cursor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collector_id, cursor)
);

ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS conversation_ingestion_id UUID
        REFERENCES knowledge.conversation_ingestions(id);
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS organization_id UUID;
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS external_attachment_id VARCHAR(255);
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS content_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS access_scope VARCHAR(32) NOT NULL DEFAULT 'conversation_members';
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS preview_capability VARCHAR(32);
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS sensitive BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS classification_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS processing_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS metadata_access_scope VARCHAR(32);
ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS content_access_scope VARCHAR(32);

-- Legacy attachments were created for uploads, so their storage metadata was
-- mandatory. Ingestion creates the metadata row before the provider content
-- is downloaded; keep those fields nullable until the content upload commits.
ALTER TABLE knowledge.attachments ALTER COLUMN mime_type DROP NOT NULL;
ALTER TABLE knowledge.attachments ALTER COLUMN object_ref DROP NOT NULL;
ALTER TABLE knowledge.attachments ALTER COLUMN content_hash DROP NOT NULL;
ALTER TABLE knowledge.attachments ALTER COLUMN metadata_access_scope DROP NOT NULL;
ALTER TABLE knowledge.attachments ALTER COLUMN content_access_scope DROP NOT NULL;
ALTER TABLE knowledge.attachments
    ALTER COLUMN metadata_access_scope SET DEFAULT 'conversation_members';
ALTER TABLE knowledge.attachments
    ALTER COLUMN content_access_scope SET DEFAULT 'conversation_members';

UPDATE knowledge.attachments AS a
SET conversation_ingestion_id = m.conversation_ingestion_id
FROM knowledge.messages AS m
WHERE a.message_id = m.id
  AND a.conversation_ingestion_id IS NULL;

UPDATE knowledge.attachments
SET content_status = CASE processing_status
    WHEN 'ready' THEN 'ready'
    WHEN 'failed' THEN 'failed'
    ELSE 'pending'
END
WHERE content_status = 'pending';

UPDATE knowledge.attachments
SET access_scope = COALESCE(
    NULLIF(content_access_scope, ''),
    NULLIF(metadata_access_scope, ''),
    'conversation_members'
)
WHERE access_scope = 'conversation_members';

UPDATE knowledge.attachments
SET metadata_access_scope = COALESCE(metadata_access_scope, access_scope, 'conversation_members'),
    content_access_scope = COALESCE(content_access_scope, access_scope, 'conversation_members')
WHERE metadata_access_scope IS NULL
   OR content_access_scope IS NULL;

-- Preserve the ingestion idempotency contract. If a legacy database already
-- contains duplicate non-null external ids, keep the first record stable and
-- make the remaining legacy rows unique before creating the full index.
WITH duplicate_attachments AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY conversation_ingestion_id, external_attachment_id
               ORDER BY created_at, id
           ) AS duplicate_number
    FROM knowledge.attachments
    WHERE conversation_ingestion_id IS NOT NULL
      AND external_attachment_id IS NOT NULL
)
UPDATE knowledge.attachments AS a
SET external_attachment_id = a.external_attachment_id || ':legacy:' || a.id::text
FROM duplicate_attachments AS d
WHERE a.id = d.id
  AND d.duplicate_number > 1;

DROP INDEX IF EXISTS knowledge.attachments_conversation_external_uq;
CREATE UNIQUE INDEX IF NOT EXISTS attachments_conversation_external_uq
    ON knowledge.attachments (conversation_ingestion_id, external_attachment_id);

-- Older outbox tables predate the aggregate/event lifecycle fields used by the
-- current Knowledge repository. Add them nullable first, backfill historical
-- rows, then enforce the runtime contract.
ALTER TABLE knowledge.outbox_events
    ADD COLUMN IF NOT EXISTS aggregate_type VARCHAR(64),
    ADD COLUMN IF NOT EXISTS aggregate_id UUID,
    ADD COLUMN IF NOT EXISTS event_version BIGINT,
    ADD COLUMN IF NOT EXISTS status VARCHAR(32),
    ADD COLUMN IF NOT EXISTS retry_count INTEGER,
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS publish_attempts INTEGER;

ALTER TABLE knowledge.outbox_events
    ALTER COLUMN aggregate_type SET DEFAULT 'legacy',
    ALTER COLUMN event_version SET DEFAULT 1,
    ALTER COLUMN status SET DEFAULT 'pending',
    ALTER COLUMN retry_count SET DEFAULT 0,
    ALTER COLUMN available_at SET DEFAULT CURRENT_TIMESTAMP;

UPDATE knowledge.outbox_events
SET aggregate_type = COALESCE(NULLIF(aggregate_type, ''), NULLIF(payload ->> 'resource_type', ''), 'legacy'),
    aggregate_id = COALESCE(
        aggregate_id,
        CASE
            WHEN (payload ->> 'resource_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
            THEN (payload ->> 'resource_id')::uuid
            ELSE id
        END
    ),
    event_version = COALESCE(event_version, 1),
    status = COALESCE(NULLIF(status, ''), CASE WHEN published_at IS NULL THEN 'pending' ELSE 'published' END),
    retry_count = COALESCE(retry_count, publish_attempts, 0),
    available_at = COALESCE(available_at, created_at, CURRENT_TIMESTAMP);

ALTER TABLE knowledge.outbox_events
    ALTER COLUMN aggregate_type SET NOT NULL,
    ALTER COLUMN aggregate_id SET NOT NULL,
    ALTER COLUMN event_version SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN retry_count SET NOT NULL,
    ALTER COLUMN available_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS outbox_events_aggregate_runtime_idx
    ON knowledge.outbox_events (aggregate_type, aggregate_id);
CREATE INDEX IF NOT EXISTS outbox_events_runtime_publish_idx
    ON knowledge.outbox_events (status, available_at);

COMMIT;
