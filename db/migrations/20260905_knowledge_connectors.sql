CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE IF NOT EXISTS knowledge.connector_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL,
    platform VARCHAR(32) NOT NULL CHECK (platform IN ('feishu', 'wecom', 'wechat')),
    platform_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    external_account_id VARCHAR(255) NOT NULL,
    display_name VARCHAR(200),
    credential_ref VARCHAR(512) NOT NULL DEFAULT '',
    token_expires_at TIMESTAMPTZ,
    default_organization_id UUID,
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'revoked', 'error')),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE knowledge.connector_accounts ADD COLUMN IF NOT EXISTS default_organization_id UUID;
CREATE UNIQUE INDEX IF NOT EXISTS connector_accounts_owner_platform_active_uq
    ON knowledge.connector_accounts (owner_user_id, platform) WHERE status <> 'revoked';
CREATE UNIQUE INDEX IF NOT EXISTS connector_accounts_external_active_uq
    ON knowledge.connector_accounts (platform, platform_workspace_key, external_account_id) WHERE status <> 'revoked';

ALTER TABLE knowledge.connector_accounts ADD COLUMN IF NOT EXISTS wechat_id VARCHAR(255);
ALTER TABLE knowledge.connector_accounts ADD COLUMN IF NOT EXISTS database_ref VARCHAR(512);
ALTER TABLE knowledge.connector_accounts ADD COLUMN IF NOT EXISTS collector_status VARCHAR(32) NOT NULL DEFAULT 'stopped';
ALTER TABLE knowledge.connector_accounts ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE knowledge.connector_accounts ADD COLUMN IF NOT EXISTS last_collected_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS knowledge.external_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform VARCHAR(32) NOT NULL CHECK (platform IN ('feishu', 'wecom', 'wechat')),
    platform_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    external_user_id VARCHAR(255) NOT NULL,
    display_name VARCHAR(200),
    avatar_url TEXT,
    mapped_user_id UUID,
    mapping_status VARCHAR(32) NOT NULL DEFAULT 'unmapped' CHECK (mapping_status IN ('unmapped', 'mapped', 'conflict')),
    mapped_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, platform_workspace_key, external_user_id)
);

CREATE TABLE IF NOT EXISTS knowledge.conversation_ingestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform VARCHAR(32) NOT NULL CHECK (platform IN ('feishu', 'wecom', 'wechat')),
    platform_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    external_conversation_id VARCHAR(255) NOT NULL,
    conversation_type VARCHAR(16) NOT NULL CHECK (conversation_type IN ('private', 'group')),
    name VARCHAR(300),
    avatar_url TEXT,
    platform_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_synced_at TIMESTAMPTZ,
    knowledge_base_id UUID,
    ingestion_scope VARCHAR(16) NOT NULL CHECK (ingestion_scope IN ('private', 'organization')),
    owner_user_id UUID,
    organization_id UUID,
    created_by_user_id UUID NOT NULL,
    requested_start_at TIMESTAMPTZ,
    effective_start_at TIMESTAMPTZ,
    permission_group_key VARCHAR(255),
    acl_version BIGINT NOT NULL DEFAULT 0 CHECK (acl_version >= 0),
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'detached', 'error')),
    pause_reason VARCHAR(100),
    detached_by_user_id UUID,
    detached_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((ingestion_scope = 'private' AND owner_user_id IS NOT NULL AND organization_id IS NULL)
        OR (ingestion_scope = 'organization' AND owner_user_id IS NULL AND organization_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS conversation_ingestions_org_active_uq
    ON knowledge.conversation_ingestions (platform, platform_workspace_key, external_conversation_id)
    WHERE ingestion_scope = 'organization' AND status IN ('active', 'paused');
CREATE UNIQUE INDEX IF NOT EXISTS conversation_ingestions_private_active_uq
    ON knowledge.conversation_ingestions (platform, platform_workspace_key, external_conversation_id, owner_user_id)
    WHERE ingestion_scope = 'private' AND status IN ('active', 'paused');

CREATE TABLE IF NOT EXISTS knowledge.conversation_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL REFERENCES knowledge.conversation_ingestions(id),
    external_identity_id UUID NOT NULL REFERENCES knowledge.external_identities(id),
    member_role VARCHAR(32),
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'left')),
    joined_at TIMESTAMPTZ,
    left_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_ingestion_id, external_identity_id)
);
CREATE INDEX IF NOT EXISTS conversation_memberships_active_idx
    ON knowledge.conversation_memberships (conversation_ingestion_id, status);
CREATE INDEX IF NOT EXISTS external_identities_mapped_user_idx
    ON knowledge.external_identities (platform, platform_workspace_key, mapped_user_id)
    WHERE mapped_user_id IS NOT NULL AND mapping_status = 'mapped';

CREATE TABLE IF NOT EXISTS knowledge.conversation_collectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL REFERENCES knowledge.conversation_ingestions(id),
    connector_account_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id),
    collector_user_id UUID NOT NULL,
    collector_role VARCHAR(16) NOT NULL CHECK (collector_role IN ('primary', 'supplemental')),
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unavailable', 'removed')),
    last_cursor TEXT,
    last_success_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_error TEXT,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS conversation_collectors_account_active_uq
    ON knowledge.conversation_collectors (conversation_ingestion_id, connector_account_id) WHERE status <> 'removed';
CREATE UNIQUE INDEX IF NOT EXISTS conversation_collectors_primary_active_uq
    ON knowledge.conversation_collectors (conversation_ingestion_id) WHERE collector_role = 'primary' AND status = 'active';

CREATE TABLE IF NOT EXISTS knowledge.messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL REFERENCES knowledge.conversation_ingestions(id),
    external_message_id VARCHAR(255) NOT NULL,
    sender_identity_id UUID REFERENCES knowledge.external_identities(id),
    sender_display_name VARCHAR(200),
    message_type VARCHAR(32) NOT NULL CHECK (message_type IN ('text', 'image', 'file', 'mixed', 'system')),
    normalized_content_ref VARCHAR(512),
    normalized_content TEXT,
    content_hash CHAR(64) NOT NULL,
    content_version INTEGER NOT NULL DEFAULT 1 CHECK (content_version >= 1),
    sent_at TIMESTAMPTZ NOT NULL,
    platform_updated_at TIMESTAMPTZ,
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active',
    vector_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_ingestion_id, external_message_id)
);
ALTER TABLE knowledge.messages ADD COLUMN IF NOT EXISTS normalized_content TEXT;
ALTER TABLE knowledge.messages ADD COLUMN IF NOT EXISTS sender_display_name VARCHAR(200);
ALTER TABLE knowledge.messages ADD COLUMN IF NOT EXISTS vector_status VARCHAR(32) NOT NULL DEFAULT 'pending';

CREATE TABLE IF NOT EXISTS knowledge.message_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES knowledge.messages(id),
    collector_id UUID NOT NULL REFERENCES knowledge.conversation_collectors(id),
    external_message_id VARCHAR(255) NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    ingest_cursor TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (message_id, collector_id),
    UNIQUE (collector_id, external_message_id)
);
ALTER TABLE knowledge.message_sources ADD COLUMN IF NOT EXISTS ingest_cursor TEXT;

-- Internal Feishu workers record one receipt only after a complete page has
-- been processed. This also covers valid empty/fully filtered pages without
-- allowing an Agent to manufacture an arbitrary cursor.
CREATE TABLE IF NOT EXISTS knowledge.collector_cursor_receipts (
    collector_id UUID NOT NULL REFERENCES knowledge.conversation_collectors(id),
    cursor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collector_id, cursor)
);

CREATE TABLE IF NOT EXISTS knowledge.attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL REFERENCES knowledge.conversation_ingestions(id),
    message_id UUID REFERENCES knowledge.messages(id),
    external_attachment_id VARCHAR(255) NOT NULL,
    file_name VARCHAR(512) NOT NULL,
    mime_type VARCHAR(255),
    size_bytes BIGINT NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    object_ref VARCHAR(512),
    content_hash CHAR(64),
    content_version INTEGER NOT NULL DEFAULT 1 CHECK (content_version >= 1),
    content_status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (content_status IN ('pending', 'ready', 'failed')),
    access_scope VARCHAR(32) NOT NULL,
    content_access_required BOOLEAN NOT NULL DEFAULT FALSE,
    preview_capability VARCHAR(32),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_ingestion_id, external_attachment_id)
);

CREATE TABLE IF NOT EXISTS knowledge.outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(100) NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trace_id VARCHAR(128) NOT NULL,
    organization_id UUID,
    producer VARCHAR(100) NOT NULL DEFAULT 'module-2',
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS outbox_events_unpublished_idx ON knowledge.outbox_events (occurred_at) WHERE published_at IS NULL;

-- Existing installations may have created this table before trace IDs became
-- mandatory. Backfill a stable legacy ID before enforcing the event contract.
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS trace_id VARCHAR(128);
ALTER TABLE knowledge.outbox_events ALTER COLUMN trace_id TYPE VARCHAR(128);
UPDATE knowledge.outbox_events
SET trace_id = 'legacy:' || id::text
WHERE trace_id IS NULL OR btrim(trace_id) = '';
ALTER TABLE knowledge.outbox_events ALTER COLUMN trace_id SET NOT NULL;

CREATE TABLE IF NOT EXISTS knowledge.wechat_pairings (
    id UUID PRIMARY KEY,
    owner_user_id UUID NOT NULL,
    code_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'consumed', 'expired', 'failed')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    wxid VARCHAR(255),
    database_ref CHAR(64),
    default_organization_id UUID,
    device_id UUID,
    connector_id UUID,
    failure_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE knowledge.wechat_pairings ADD COLUMN IF NOT EXISTS default_organization_id UUID;
ALTER TABLE knowledge.wechat_pairings ADD COLUMN IF NOT EXISTS failure_code VARCHAR(64);
ALTER TABLE knowledge.wechat_pairings DROP CONSTRAINT IF EXISTS wechat_pairings_status_check;
ALTER TABLE knowledge.wechat_pairings ADD CONSTRAINT wechat_pairings_status_check CHECK (status IN ('pending', 'consumed', 'expired', 'failed'));

CREATE TABLE IF NOT EXISTS knowledge.agent_devices (
    id UUID PRIMARY KEY,
    connector_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id),
    owner_user_id UUID NOT NULL,
    key_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    agent_version VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge.wechat_collection_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_account_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id) ON DELETE CASCADE,
    selected_conversations JSONB NOT NULL DEFAULT '[]'::jsonb,
    history_start_at TIMESTAMPTZ,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    listen_mode VARCHAR(32) NOT NULL DEFAULT 'whitelist',
    config_version BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (jsonb_typeof(selected_conversations) = 'array'),
    CHECK (listen_mode IN ('whitelist', 'all') )
);
CREATE UNIQUE INDEX IF NOT EXISTS wechat_collection_configs_account_uq ON knowledge.wechat_collection_configs (connector_account_id);

CREATE TABLE IF NOT EXISTS knowledge.wechat_collector_runtime (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_account_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL DEFAULT 'stopped',
    collector_version VARCHAR(100),
    last_heartbeat_at TIMESTAMPTZ,
    last_collected_at TIMESTAMPTZ,
    processed_count BIGINT NOT NULL DEFAULT 0,
    failed_count BIGINT NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('stopped', 'starting', 'running', 'paused', 'error')),
    CHECK (processed_count >= 0), CHECK (failed_count >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS wechat_collector_runtime_account_uq ON knowledge.wechat_collector_runtime (connector_account_id);
CREATE INDEX IF NOT EXISTS wechat_collector_runtime_heartbeat_idx ON knowledge.wechat_collector_runtime (last_heartbeat_at);
