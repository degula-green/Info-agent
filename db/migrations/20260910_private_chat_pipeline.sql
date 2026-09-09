BEGIN;

ALTER TABLE knowledge.messages
    ADD COLUMN IF NOT EXISTS sensitive BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS classification_status VARCHAR(32) NOT NULL DEFAULT 'succeeded';

ALTER TABLE knowledge.attachments
    ADD COLUMN IF NOT EXISTS sensitive BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS classification_status VARCHAR(32) NOT NULL DEFAULT 'succeeded';

CREATE TABLE IF NOT EXISTS knowledge.message_private_content (
    message_id UUID PRIMARY KEY REFERENCES knowledge.messages(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge.private_share_references (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    source_private_resource_id UUID NOT NULL,
    source_resource_type VARCHAR(16) NOT NULL CHECK (source_resource_type IN ('message', 'attachment')),
    source_content_version INTEGER NOT NULL DEFAULT 1 CHECK (source_content_version >= 1),
    share_batch_id UUID NOT NULL,
    share_request_id VARCHAR(100) NOT NULL,
    created_by_user_id UUID NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'revoked')),
    sensitive BOOLEAN NOT NULL DEFAULT FALSE,
    content_access_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, source_private_resource_id, source_resource_type, source_content_version)
);

CREATE INDEX IF NOT EXISTS private_share_references_source_idx
    ON knowledge.private_share_references (source_private_resource_id, source_resource_type);

CREATE TABLE IF NOT EXISTS knowledge.private_access_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_user_id UUID NOT NULL,
    share_reference_id UUID NOT NULL REFERENCES knowledge.private_share_references(id),
    resource_id UUID NOT NULL,
    resource_type VARCHAR(16) NOT NULL CHECK (resource_type IN ('message', 'attachment')),
    requested_action VARCHAR(16) NOT NULL CHECK (requested_action IN ('view', 'download')),
    reason TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'revoked')),
    reviewed_by_user_id UUID,
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS private_access_requests_pending_uq
    ON knowledge.private_access_requests (requester_user_id, share_reference_id, resource_id, requested_action)
    WHERE status = 'pending';

COMMIT;
