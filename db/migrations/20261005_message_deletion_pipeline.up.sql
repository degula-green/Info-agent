BEGIN;

ALTER TABLE knowledge.messages
    DROP CONSTRAINT IF EXISTS messages_lifecycle_chk;
ALTER TABLE knowledge.messages
    ADD CONSTRAINT messages_lifecycle_chk
    CHECK (lifecycle_status IN ('active', 'deleting', 'deleted', 'purged'));

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_lifecycle_chk;
ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_lifecycle_chk
    CHECK (lifecycle_status IN ('active', 'deleting', 'deleted', 'purged'));

ALTER TABLE knowledge.messages
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by_user_id UUID,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT,
    ADD COLUMN IF NOT EXISTS delete_request_id UUID,
    ADD COLUMN IF NOT EXISTS purge_after TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS content_purged_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS vector_delete_status VARCHAR(32) NOT NULL DEFAULT 'pending';

ALTER TABLE knowledge.knowledge_items
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by_user_id UUID,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT,
    ADD COLUMN IF NOT EXISTS delete_request_id UUID,
    ADD COLUMN IF NOT EXISTS purge_after TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS content_purged_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS vector_delete_status VARCHAR(32) NOT NULL DEFAULT 'pending';

ALTER TABLE knowledge.messages
    ADD CONSTRAINT messages_vector_delete_status_chk
    CHECK (vector_delete_status IN ('pending', 'deleting', 'deleted', 'failed', 'not_required')) NOT VALID;
ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_vector_delete_status_chk
    CHECK (vector_delete_status IN ('pending', 'deleting', 'deleted', 'failed', 'not_required')) NOT VALID;

CREATE INDEX IF NOT EXISTS messages_lifecycle_idx
    ON knowledge.messages (lifecycle_status, purge_after);
CREATE INDEX IF NOT EXISTS messages_delete_request_idx
    ON knowledge.messages (delete_request_id);
CREATE INDEX IF NOT EXISTS knowledge_items_lifecycle_idx
    ON knowledge.knowledge_items (lifecycle_status, purge_after);
CREATE INDEX IF NOT EXISTS knowledge_items_delete_request_idx
    ON knowledge.knowledge_items (delete_request_id);

CREATE TABLE IF NOT EXISTS knowledge.deletion_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID,
    requester_user_id UUID NOT NULL,
    reviewer_user_id UUID,
    scope_type VARCHAR(32) NOT NULL,
    scope_id UUID NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMPTZ,
    execution_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    purge_after TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT deletion_requests_scope_chk
        CHECK (scope_type IN ('message', 'conversation', 'identity', 'derived_index')),
    CONSTRAINT deletion_requests_status_chk
        CHECK (status IN ('pending', 'approved', 'rejected', 'executing', 'completed', 'failed')),
    CONSTRAINT deletion_requests_idempotency_uq
        UNIQUE (requester_user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS deletion_requests_requester_idx
    ON knowledge.deletion_requests (requester_user_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS deletion_requests_org_status_idx
    ON knowledge.deletion_requests (organization_id, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS deletion_requests_scope_idx
    ON knowledge.deletion_requests (scope_type, scope_id);

CREATE TABLE IF NOT EXISTS knowledge.deletion_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deletion_request_id UUID NOT NULL
        REFERENCES knowledge.deletion_requests (id) ON DELETE CASCADE,
    resource_type VARCHAR(32) NOT NULL,
    resource_id UUID NOT NULL,
    knowledge_item_id UUID,
    conversation_ingestion_id UUID,
    content_version INTEGER NOT NULL DEFAULT 1,
    acl_version BIGINT NOT NULL DEFAULT 0,
    visibility_state VARCHAR(16) NOT NULL DEFAULT 'active',
    vector_state VARCHAR(16) NOT NULL DEFAULT 'pending',
    object_state VARCHAR(16) NOT NULL DEFAULT 'pending',
    auth_state VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT deletion_targets_resource_type_chk
        CHECK (resource_type IN ('message', 'attachment', 'knowledge_item')),
    CONSTRAINT deletion_targets_visibility_chk
        CHECK (visibility_state IN ('active', 'hidden')),
    CONSTRAINT deletion_targets_vector_chk
        CHECK (vector_state IN ('pending', 'deleting', 'deleted', 'failed', 'not_required')),
    CONSTRAINT deletion_targets_object_chk
        CHECK (object_state IN ('pending', 'deleting', 'deleted', 'skipped', 'failed')),
    CONSTRAINT deletion_targets_auth_chk
        CHECK (auth_state IN ('pending', 'revoked', 'failed')),
    CONSTRAINT deletion_targets_attempt_chk CHECK (attempt_count >= 0),
    CONSTRAINT deletion_targets_resource_uq
        UNIQUE (deletion_request_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS deletion_targets_request_idx
    ON knowledge.deletion_targets (deletion_request_id, visibility_state, vector_state);
CREATE INDEX IF NOT EXISTS deletion_targets_item_idx
    ON knowledge.deletion_targets (knowledge_item_id);
CREATE INDEX IF NOT EXISTS deletion_targets_resource_idx
    ON knowledge.deletion_targets (resource_type, resource_id);

CREATE TABLE IF NOT EXISTS knowledge.deletion_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deletion_request_id UUID NOT NULL
        REFERENCES knowledge.deletion_requests (id) ON DELETE CASCADE,
    actor_user_id UUID,
    action VARCHAR(64) NOT NULL,
    resource_type VARCHAR(32),
    resource_id UUID,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS deletion_audit_request_idx
    ON knowledge.deletion_audit_logs (deletion_request_id, created_at);

COMMIT;
