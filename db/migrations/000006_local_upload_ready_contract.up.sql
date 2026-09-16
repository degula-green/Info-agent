-- Align local-upload resources with the service-two Ready contract. Existing
-- rows remain queryable; only future finalized uploads enter the Ready flow.
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS request_id VARCHAR(128);
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS uploaded_by_user_id UUID;
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS upload_destination VARCHAR(64);
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS organization_id UUID;
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS metadata_access_scope VARCHAR(32);
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS content_access_scope VARCHAR(32);
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS upload_status VARCHAR(32) NOT NULL DEFAULT 'uploaded';
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS upload_error TEXT;
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS processing_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.attachments ADD COLUMN IF NOT EXISTS content_access_required BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS attachments_request_id_unique ON knowledge.attachments (request_id) WHERE request_id IS NOT NULL;
-- Only the canonical uploaded row is unique. Duplicate tasks remain auditable
-- records and reuse its object reference without violating this constraint.
CREATE UNIQUE INDEX IF NOT EXISTS attachments_private_hash_unique ON knowledge.attachments (uploaded_by_user_id, content_hash) WHERE upload_destination='private_local_library' AND upload_status='uploaded';
CREATE UNIQUE INDEX IF NOT EXISTS attachments_organization_hash_unique ON knowledge.attachments (organization_id, content_hash) WHERE upload_destination='organization_file_library' AND upload_status='uploaded';

ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS acl_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_visibility VARCHAR(32) NOT NULL DEFAULT 'display';

ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS aggregate_type VARCHAR(64);
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS aggregate_id UUID;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS event_version INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_local_ready_once ON knowledge.outbox_events (aggregate_id, event_type) WHERE event_type='knowledge.ready';
CREATE INDEX IF NOT EXISTS outbox_ready_relay ON knowledge.outbox_events (available_at, occurred_at) WHERE event_type='knowledge.ready' AND published_at IS NULL;
