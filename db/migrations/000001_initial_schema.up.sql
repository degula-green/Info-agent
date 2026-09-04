BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS iam;
CREATE SCHEMA IF NOT EXISTS knowledge;
CREATE SCHEMA IF NOT EXISTS rag;

-- IAM schema

CREATE TABLE iam.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(320) NOT NULL,
    nickname VARCHAR(100) NOT NULL,
    avatar_object_key VARCHAR(512),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    email_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT users_status_chk CHECK (status IN ('pending', 'active', 'disabled'))
);

CREATE UNIQUE INDEX users_email_active_uq
    ON iam.users (lower(email))
    WHERE deleted_at IS NULL;
CREATE INDEX users_status_idx ON iam.users (status);
CREATE INDEX users_deleted_at_idx ON iam.users (deleted_at);

CREATE TABLE iam.user_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES iam.users (id),
    credential_type VARCHAR(32) NOT NULL DEFAULT 'password',
    credential_key VARCHAR(320) NOT NULL,
    password_hash VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    password_changed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT user_credentials_type_chk
        CHECK (credential_type IN ('password', 'oidc', 'sso')),
    CONSTRAINT user_credentials_status_chk
        CHECK (status IN ('active', 'disabled')),
    CONSTRAINT user_credentials_password_chk
        CHECK (credential_type <> 'password' OR password_hash IS NOT NULL)
);

CREATE UNIQUE INDEX user_credentials_key_uq
    ON iam.user_credentials (credential_type, lower(credential_key));
CREATE INDEX user_credentials_user_idx ON iam.user_credentials (user_id);

CREATE TABLE iam.organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(100) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_by_user_id UUID NOT NULL REFERENCES iam.users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dissolved_at TIMESTAMPTZ,
    CONSTRAINT organizations_status_chk
        CHECK (status IN ('active', 'suspended', 'dissolved'))
);

CREATE UNIQUE INDEX organizations_slug_uq ON iam.organizations (lower(slug));
CREATE INDEX organizations_status_idx ON iam.organizations (status);
CREATE INDEX organizations_creator_idx ON iam.organizations (created_by_user_id);

CREATE TABLE iam.organization_invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES iam.organizations (id),
    created_by_user_id UUID NOT NULL REFERENCES iam.users (id),
    token_hash CHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_by_user_id UUID REFERENCES iam.users (id),
    accepted_at TIMESTAMPTZ,
    revoked_by_user_id UUID REFERENCES iam.users (id),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT organization_invitations_status_chk
        CHECK (status IN ('pending', 'accepted', 'revoked', 'expired'))
);

CREATE UNIQUE INDEX organization_invitations_token_uq
    ON iam.organization_invitations (token_hash);
CREATE INDEX organization_invitations_org_idx
    ON iam.organization_invitations (organization_id);
CREATE INDEX organization_invitations_status_idx
    ON iam.organization_invitations (status);
CREATE INDEX organization_invitations_expires_idx
    ON iam.organization_invitations (expires_at);

CREATE TABLE iam.organization_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES iam.organizations (id),
    user_id UUID NOT NULL REFERENCES iam.users (id),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    joined_via VARCHAR(32) NOT NULL DEFAULT 'created',
    invitation_id UUID REFERENCES iam.organization_invitations (id),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    exit_reason TEXT,
    exit_requested_at TIMESTAMPTZ,
    exit_reviewed_by_user_id UUID REFERENCES iam.users (id),
    exit_review_note TEXT,
    exit_reviewed_at TIMESTAMPTZ,
    left_at TIMESTAMPTZ,
    suspended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT organization_memberships_org_user_uq
        UNIQUE (organization_id, user_id),
    CONSTRAINT organization_memberships_status_chk
        CHECK (status IN ('active', 'suspended', 'leaving', 'left')),
    CONSTRAINT organization_memberships_joined_via_chk
        CHECK (joined_via IN ('created', 'invitation')),
    CONSTRAINT organization_memberships_invitation_chk
        CHECK (
            (joined_via = 'created' AND invitation_id IS NULL)
            OR (joined_via = 'invitation' AND invitation_id IS NOT NULL)
        )
);

CREATE UNIQUE INDEX organization_memberships_one_active_org_uq
    ON iam.organization_memberships (user_id)
    WHERE status IN ('active', 'suspended', 'leaving');
CREATE INDEX organization_memberships_org_idx
    ON iam.organization_memberships (organization_id);
CREATE INDEX organization_memberships_user_idx
    ON iam.organization_memberships (user_id);
CREATE INDEX organization_memberships_status_idx
    ON iam.organization_memberships (status);
CREATE INDEX organization_memberships_joined_at_idx
    ON iam.organization_memberships (joined_at);
CREATE INDEX organization_memberships_exit_requested_idx
    ON iam.organization_memberships (exit_requested_at);
CREATE INDEX organization_memberships_left_at_idx
    ON iam.organization_memberships (left_at);

CREATE TABLE iam.membership_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    membership_id UUID NOT NULL REFERENCES iam.organization_memberships (id),
    role_code VARCHAR(64) NOT NULL,
    granted_by_user_id UUID NOT NULL REFERENCES iam.users (id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_by_user_id UUID REFERENCES iam.users (id),
    revoked_at TIMESTAMPTZ,
    CONSTRAINT membership_roles_role_code_chk
        CHECK (role_code IN ('owner', 'information_admin', 'membership_approver', 'member'))
);

CREATE UNIQUE INDEX membership_roles_active_uq
    ON iam.membership_roles (membership_id, role_code)
    WHERE revoked_at IS NULL;
CREATE INDEX membership_roles_membership_idx
    ON iam.membership_roles (membership_id);
CREATE INDEX membership_roles_revoked_at_idx
    ON iam.membership_roles (revoked_at);

CREATE TABLE iam.access_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES iam.organizations (id),
    requester_user_id UUID NOT NULL REFERENCES iam.users (id),
    resource_scope VARCHAR(16) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    action VARCHAR(16) NOT NULL,
    reason TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    request_expires_at TIMESTAMPTZ,
    reviewed_by_user_id UUID REFERENCES iam.users (id),
    reviewer_basis VARCHAR(32),
    review_note TEXT,
    reviewed_at TIMESTAMPTZ,
    grant_expires_at TIMESTAMPTZ,
    fga_sync_status VARCHAR(32) NOT NULL DEFAULT 'not_started',
    fga_tuple_key VARCHAR(512),
    granted_at TIMESTAMPTZ,
    revoked_by_user_id UUID REFERENCES iam.users (id),
    revoked_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT access_requests_scope_chk
        CHECK (resource_scope IN ('private', 'organization')),
    CONSTRAINT access_requests_resource_type_chk
        CHECK (resource_type IN ('knowledge_original', 'attachment_content')),
    CONSTRAINT access_requests_action_chk
        CHECK (action IN ('view', 'download')),
    CONSTRAINT access_requests_original_action_chk
        CHECK (resource_type <> 'knowledge_original' OR action = 'view'),
    CONSTRAINT access_requests_status_chk
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired', 'revoked')),
    CONSTRAINT access_requests_reviewer_basis_chk
        CHECK (reviewer_basis IS NULL OR reviewer_basis IN ('information_admin', 'collector')),
    CONSTRAINT access_requests_fga_status_chk
        CHECK (fga_sync_status IN ('not_started', 'pending', 'synced', 'failed', 'removed')),
    CONSTRAINT access_requests_org_scope_chk
        CHECK (
            (resource_scope = 'private' AND organization_id IS NULL)
            OR (resource_scope = 'organization' AND organization_id IS NOT NULL)
        )
);

CREATE UNIQUE INDEX access_requests_pending_uq
    ON iam.access_requests (requester_user_id, resource_type, resource_id, action)
    WHERE status = 'pending';
CREATE INDEX access_requests_org_idx ON iam.access_requests (organization_id);
CREATE INDEX access_requests_requester_idx ON iam.access_requests (requester_user_id);
CREATE INDEX access_requests_scope_idx ON iam.access_requests (resource_scope);
CREATE INDEX access_requests_resource_idx
    ON iam.access_requests (resource_type, resource_id);
CREATE INDEX access_requests_status_idx ON iam.access_requests (status);
CREATE INDEX access_requests_request_expires_idx
    ON iam.access_requests (request_expires_at);
CREATE INDEX access_requests_grant_expires_idx
    ON iam.access_requests (grant_expires_at);
CREATE INDEX access_requests_fga_status_idx
    ON iam.access_requests (fga_sync_status);
CREATE INDEX access_requests_granted_at_idx ON iam.access_requests (granted_at);
CREATE INDEX access_requests_created_at_idx ON iam.access_requests (created_at);

CREATE TABLE iam.audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID REFERENCES iam.users (id),
    organization_id UUID REFERENCES iam.organizations (id),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(64),
    resource_id UUID,
    result VARCHAR(32) NOT NULL DEFAULT 'success',
    request_id VARCHAR(100),
    ip_address INET,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT audit_logs_result_chk
        CHECK (result IN ('success', 'failed', 'denied'))
);

CREATE INDEX audit_logs_actor_idx ON iam.audit_logs (actor_user_id);
CREATE INDEX audit_logs_org_idx ON iam.audit_logs (organization_id);
CREATE INDEX audit_logs_action_idx ON iam.audit_logs (action);
CREATE INDEX audit_logs_resource_idx ON iam.audit_logs (resource_type, resource_id);
CREATE INDEX audit_logs_request_idx ON iam.audit_logs (request_id);
CREATE INDEX audit_logs_created_at_idx ON iam.audit_logs (created_at);

-- Knowledge schema. IAM identifiers in this schema are logical references by design.

CREATE TABLE knowledge.knowledge_bases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_scope VARCHAR(16) NOT NULL,
    base_type VARCHAR(32) NOT NULL,
    name VARCHAR(200) NOT NULL,
    owner_user_id UUID,
    organization_id UUID,
    source_key VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT knowledge_bases_scope_chk
        CHECK (knowledge_scope IN ('private', 'organization')),
    CONSTRAINT knowledge_bases_type_chk
        CHECK (base_type IN (
            'private_conversation',
            'private_local',
            'organization_conversation',
            'organization_files'
        )),
    CONSTRAINT knowledge_bases_status_chk
        CHECK (status IN ('active', 'archived')),
    CONSTRAINT knowledge_bases_owner_chk
        CHECK (
            (knowledge_scope = 'private'
                AND owner_user_id IS NOT NULL
                AND organization_id IS NULL
                AND base_type IN ('private_conversation', 'private_local'))
            OR
            (knowledge_scope = 'organization'
                AND owner_user_id IS NULL
                AND organization_id IS NOT NULL
                AND base_type IN ('organization_conversation', 'organization_files'))
        )
);

CREATE UNIQUE INDEX knowledge_bases_private_local_uq
    ON knowledge.knowledge_bases (owner_user_id)
    WHERE base_type = 'private_local';
CREATE UNIQUE INDEX knowledge_bases_org_files_uq
    ON knowledge.knowledge_bases (organization_id)
    WHERE base_type = 'organization_files';
CREATE UNIQUE INDEX knowledge_bases_private_source_uq
    ON knowledge.knowledge_bases (owner_user_id, base_type, source_key)
    WHERE knowledge_scope = 'private' AND source_key IS NOT NULL;
CREATE UNIQUE INDEX knowledge_bases_org_source_uq
    ON knowledge.knowledge_bases (organization_id, base_type, source_key)
    WHERE knowledge_scope = 'organization' AND source_key IS NOT NULL;
CREATE INDEX knowledge_bases_owner_idx ON knowledge.knowledge_bases (owner_user_id);
CREATE INDEX knowledge_bases_org_idx ON knowledge.knowledge_bases (organization_id);
CREATE INDEX knowledge_bases_status_idx ON knowledge.knowledge_bases (status);

CREATE TABLE knowledge.connector_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL,
    platform VARCHAR(32) NOT NULL,
    platform_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    external_account_id VARCHAR(255) NOT NULL,
    display_name VARCHAR(200),
    credential_ref VARCHAR(512) NOT NULL,
    token_expires_at TIMESTAMPTZ,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT connector_accounts_external_uq
        UNIQUE (platform, platform_workspace_key, external_account_id),
    CONSTRAINT connector_accounts_platform_chk
        CHECK (platform IN ('feishu', 'wecom', 'wechat')),
    CONSTRAINT connector_accounts_status_chk
        CHECK (status IN ('active', 'expired', 'revoked', 'error'))
);

CREATE INDEX connector_accounts_owner_idx
    ON knowledge.connector_accounts (owner_user_id);
CREATE INDEX connector_accounts_token_expires_idx
    ON knowledge.connector_accounts (token_expires_at);
CREATE INDEX connector_accounts_status_idx
    ON knowledge.connector_accounts (status);

CREATE TABLE knowledge.external_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform VARCHAR(32) NOT NULL,
    platform_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    external_user_id VARCHAR(255) NOT NULL,
    display_name VARCHAR(200),
    avatar_url TEXT,
    mapped_user_id UUID,
    mapping_status VARCHAR(32) NOT NULL DEFAULT 'unmapped',
    mapped_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT external_identities_external_uq
        UNIQUE (platform, platform_workspace_key, external_user_id),
    CONSTRAINT external_identities_platform_chk
        CHECK (platform IN ('feishu', 'wecom', 'wechat')),
    CONSTRAINT external_identities_mapping_status_chk
        CHECK (mapping_status IN ('unmapped', 'mapped', 'conflict')),
    CONSTRAINT external_identities_mapped_user_chk
        CHECK (mapping_status <> 'mapped' OR mapped_user_id IS NOT NULL)
);

CREATE INDEX external_identities_mapped_user_idx
    ON knowledge.external_identities (mapped_user_id);
CREATE INDEX external_identities_mapping_status_idx
    ON knowledge.external_identities (mapping_status);

CREATE TABLE knowledge.conversation_ingestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform VARCHAR(32) NOT NULL,
    platform_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    external_conversation_id VARCHAR(255) NOT NULL,
    conversation_type VARCHAR(16) NOT NULL,
    name VARCHAR(300),
    avatar_url TEXT,
    platform_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_synced_at TIMESTAMPTZ,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge.knowledge_bases (id),
    ingestion_scope VARCHAR(16) NOT NULL,
    owner_user_id UUID,
    organization_id UUID,
    created_by_user_id UUID NOT NULL,
    requested_start_at TIMESTAMPTZ,
    effective_start_at TIMESTAMPTZ,
    permission_group_key VARCHAR(255),
    acl_version BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    pause_reason VARCHAR(100),
    detached_by_user_id UUID,
    detached_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT conversation_ingestions_platform_chk
        CHECK (platform IN ('feishu', 'wecom', 'wechat')),
    CONSTRAINT conversation_ingestions_type_chk
        CHECK (conversation_type IN ('private', 'group')),
    CONSTRAINT conversation_ingestions_scope_chk
        CHECK (ingestion_scope IN ('private', 'organization')),
    CONSTRAINT conversation_ingestions_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT conversation_ingestions_status_chk
        CHECK (status IN ('active', 'paused', 'detached', 'error')),
    CONSTRAINT conversation_ingestions_owner_chk
        CHECK (
            (ingestion_scope = 'private'
                AND conversation_type = 'private'
                AND owner_user_id IS NOT NULL
                AND organization_id IS NULL
                AND permission_group_key IS NULL)
            OR
            (ingestion_scope = 'organization'
                AND conversation_type = 'group'
                AND owner_user_id IS NULL
                AND organization_id IS NOT NULL
                AND permission_group_key IS NOT NULL)
        )
);

CREATE UNIQUE INDEX conversation_ingestions_group_active_uq
    ON knowledge.conversation_ingestions (
        platform,
        platform_workspace_key,
        external_conversation_id
    )
    WHERE ingestion_scope = 'organization' AND status IN ('active', 'paused');
CREATE UNIQUE INDEX conversation_ingestions_private_active_uq
    ON knowledge.conversation_ingestions (
        platform,
        platform_workspace_key,
        external_conversation_id,
        owner_user_id
    )
    WHERE ingestion_scope = 'private' AND status IN ('active', 'paused');
CREATE UNIQUE INDEX conversation_ingestions_permission_group_uq
    ON knowledge.conversation_ingestions (permission_group_key)
    WHERE permission_group_key IS NOT NULL;
CREATE INDEX conversation_ingestions_base_idx
    ON knowledge.conversation_ingestions (knowledge_base_id);
CREATE INDEX conversation_ingestions_owner_idx
    ON knowledge.conversation_ingestions (owner_user_id);
CREATE INDEX conversation_ingestions_org_idx
    ON knowledge.conversation_ingestions (organization_id);
CREATE INDEX conversation_ingestions_creator_idx
    ON knowledge.conversation_ingestions (created_by_user_id);
CREATE INDEX conversation_ingestions_status_idx
    ON knowledge.conversation_ingestions (status);
CREATE INDEX conversation_ingestions_last_synced_idx
    ON knowledge.conversation_ingestions (last_synced_at);
CREATE INDEX conversation_ingestions_detached_at_idx
    ON knowledge.conversation_ingestions (detached_at);

CREATE TABLE knowledge.conversation_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL
        REFERENCES knowledge.conversation_ingestions (id),
    external_identity_id UUID NOT NULL
        REFERENCES knowledge.external_identities (id),
    member_role VARCHAR(32),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    joined_at TIMESTAMPTZ,
    left_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT conversation_memberships_identity_uq
        UNIQUE (conversation_ingestion_id, external_identity_id),
    CONSTRAINT conversation_memberships_status_chk
        CHECK (status IN ('active', 'left'))
);

CREATE INDEX conversation_memberships_ingestion_idx
    ON knowledge.conversation_memberships (conversation_ingestion_id);
CREATE INDEX conversation_memberships_identity_idx
    ON knowledge.conversation_memberships (external_identity_id);
CREATE INDEX conversation_memberships_status_idx
    ON knowledge.conversation_memberships (status);
CREATE INDEX conversation_memberships_left_at_idx
    ON knowledge.conversation_memberships (left_at);
CREATE INDEX conversation_memberships_last_seen_idx
    ON knowledge.conversation_memberships (last_seen_at);

CREATE TABLE knowledge.conversation_collectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL
        REFERENCES knowledge.conversation_ingestions (id),
    connector_account_id UUID NOT NULL REFERENCES knowledge.connector_accounts (id),
    collector_user_id UUID NOT NULL,
    collector_role VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    last_cursor TEXT,
    last_success_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT conversation_collectors_role_chk
        CHECK (collector_role IN ('primary', 'supplemental')),
    CONSTRAINT conversation_collectors_status_chk
        CHECK (status IN ('active', 'unavailable', 'removed')),
    CONSTRAINT conversation_collectors_failures_chk CHECK (consecutive_failures >= 0)
);

CREATE UNIQUE INDEX conversation_collectors_account_active_uq
    ON knowledge.conversation_collectors (
        conversation_ingestion_id,
        connector_account_id
    )
    WHERE status <> 'removed';
CREATE UNIQUE INDEX conversation_collectors_primary_active_uq
    ON knowledge.conversation_collectors (conversation_ingestion_id)
    WHERE collector_role = 'primary' AND status = 'active';
CREATE INDEX conversation_collectors_ingestion_idx
    ON knowledge.conversation_collectors (conversation_ingestion_id);
CREATE INDEX conversation_collectors_account_idx
    ON knowledge.conversation_collectors (connector_account_id);
CREATE INDEX conversation_collectors_user_idx
    ON knowledge.conversation_collectors (collector_user_id);
CREATE INDEX conversation_collectors_status_idx
    ON knowledge.conversation_collectors (status);
CREATE INDEX conversation_collectors_last_success_idx
    ON knowledge.conversation_collectors (last_success_at);
CREATE INDEX conversation_collectors_poll_idx
    ON knowledge.conversation_collectors (status, next_poll_at);

CREATE TABLE knowledge.messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_ingestion_id UUID NOT NULL
        REFERENCES knowledge.conversation_ingestions (id),
    external_message_id VARCHAR(255) NOT NULL,
    sender_identity_id UUID REFERENCES knowledge.external_identities (id),
    message_type VARCHAR(32) NOT NULL,
    normalized_content_ref VARCHAR(512),
    content_hash CHAR(64) NOT NULL,
    content_version INTEGER NOT NULL DEFAULT 1,
    sent_at TIMESTAMPTZ NOT NULL,
    platform_updated_at TIMESTAMPTZ,
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT messages_external_uq
        UNIQUE (conversation_ingestion_id, external_message_id),
    CONSTRAINT messages_type_chk
        CHECK (message_type IN ('text', 'image', 'file', 'mixed', 'system')),
    CONSTRAINT messages_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT messages_lifecycle_chk CHECK (lifecycle_status = 'active')
);

CREATE INDEX messages_ingestion_sent_idx
    ON knowledge.messages (conversation_ingestion_id, sent_at);
CREATE INDEX messages_sender_idx ON knowledge.messages (sender_identity_id);
CREATE INDEX messages_content_hash_idx ON knowledge.messages (content_hash);
CREATE INDEX messages_created_at_idx ON knowledge.messages (created_at);

CREATE TABLE knowledge.message_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES knowledge.messages (id),
    collector_id UUID NOT NULL REFERENCES knowledge.conversation_collectors (id),
    external_message_id VARCHAR(255) NOT NULL,
    raw_payload_ref VARCHAR(512),
    payload_hash CHAR(64) NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT message_sources_collector_external_uq
        UNIQUE (collector_id, external_message_id),
    CONSTRAINT message_sources_message_collector_uq
        UNIQUE (message_id, collector_id)
);

CREATE INDEX message_sources_message_idx ON knowledge.message_sources (message_id);
CREATE INDEX message_sources_collector_idx ON knowledge.message_sources (collector_id);
CREATE INDEX message_sources_collected_at_idx
    ON knowledge.message_sources (collected_at);

CREATE TABLE knowledge.attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id VARCHAR(100),
    message_id UUID REFERENCES knowledge.messages (id),
    uploaded_by_user_id UUID,
    upload_destination VARCHAR(32),
    organization_id UUID,
    file_name VARCHAR(500) NOT NULL,
    mime_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    object_ref VARCHAR(512) NOT NULL,
    source_private_attachment_id UUID REFERENCES knowledge.attachments (id),
    content_hash CHAR(64) NOT NULL,
    content_version INTEGER NOT NULL DEFAULT 1,
    metadata_access_scope VARCHAR(32) NOT NULL,
    content_access_scope VARCHAR(32) NOT NULL,
    acl_version BIGINT NOT NULL DEFAULT 0,
    encrypted BOOLEAN NOT NULL DEFAULT FALSE,
    content_access_required BOOLEAN NOT NULL DEFAULT FALSE,
    upload_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    upload_error TEXT,
    uploaded_at TIMESTAMPTZ,
    extracted_original_ref VARCHAR(512),
    extracted_display_ref VARCHAR(512),
    processing_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    sensitivity VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT attachments_source_chk
        CHECK (message_id IS NOT NULL OR uploaded_by_user_id IS NOT NULL),
    CONSTRAINT attachments_destination_chk
        CHECK (
            upload_destination IS NULL
            OR upload_destination IN ('private_local_library', 'organization_file_library')
        ),
    CONSTRAINT attachments_local_upload_chk
        CHECK (
            upload_destination IS NULL
            OR (request_id IS NOT NULL AND uploaded_by_user_id IS NOT NULL)
        ),
    CONSTRAINT attachments_size_chk CHECK (size_bytes >= 0),
    CONSTRAINT attachments_source_private_chk
        CHECK (source_private_attachment_id IS NULL OR source_private_attachment_id <> id),
    CONSTRAINT attachments_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT attachments_metadata_scope_chk
        CHECK (metadata_access_scope IN (
            'owner_only', 'organization_members', 'conversation_members'
        )),
    CONSTRAINT attachments_content_scope_chk
        CHECK (content_access_scope IN (
            'owner_only', 'organization_members', 'conversation_members'
        )),
    CONSTRAINT attachments_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT attachments_protected_scope_chk
        CHECK (
            NOT content_access_required
            OR (
                organization_id IS NOT NULL
                AND metadata_access_scope = 'conversation_members'
                AND content_access_scope = 'conversation_members'
            )
        ),
    CONSTRAINT attachments_upload_status_chk
        CHECK (upload_status IN (
            'pending', 'validating', 'uploading', 'uploaded', 'failed', 'duplicate'
        )),
    CONSTRAINT attachments_processing_status_chk
        CHECK (processing_status IN ('pending', 'processing', 'ready', 'failed')),
    CONSTRAINT attachments_sensitivity_chk
        CHECK (
            sensitivity IS NULL
            OR sensitivity IN ('public', 'internal', 'confidential', 'restricted')
        )
);

CREATE UNIQUE INDEX attachments_local_request_uq
    ON knowledge.attachments (request_id)
    WHERE upload_destination IS NOT NULL;
-- Logical shared attachments may intentionally reuse the same immutable object.
CREATE INDEX attachments_object_ref_idx ON knowledge.attachments (object_ref);
CREATE INDEX attachments_message_idx ON knowledge.attachments (message_id);
CREATE INDEX attachments_uploader_idx ON knowledge.attachments (uploaded_by_user_id);
CREATE INDEX attachments_org_idx ON knowledge.attachments (organization_id);
CREATE INDEX attachments_mime_type_idx ON knowledge.attachments (mime_type);
CREATE INDEX attachments_source_private_idx
    ON knowledge.attachments (source_private_attachment_id);
CREATE INDEX attachments_content_hash_idx ON knowledge.attachments (content_hash);
CREATE INDEX attachments_access_required_idx
    ON knowledge.attachments (content_access_required);
CREATE INDEX attachments_upload_status_idx ON knowledge.attachments (upload_status);
CREATE INDEX attachments_processing_status_idx
    ON knowledge.attachments (processing_status);
CREATE INDEX attachments_sensitivity_idx ON knowledge.attachments (sensitivity);
CREATE INDEX attachments_created_at_idx ON knowledge.attachments (created_at);

CREATE TABLE knowledge.knowledge_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_base_id UUID NOT NULL REFERENCES knowledge.knowledge_bases (id),
    knowledge_scope VARCHAR(16) NOT NULL,
    access_scope VARCHAR(32) NOT NULL,
    owner_user_id UUID,
    organization_id UUID,
    conversation_ingestion_id UUID REFERENCES knowledge.conversation_ingestions (id),
    source_type VARCHAR(32) NOT NULL,
    source_message_id UUID REFERENCES knowledge.messages (id),
    source_attachment_id UUID REFERENCES knowledge.attachments (id),
    source_private_item_id UUID REFERENCES knowledge.knowledge_items (id),
    share_request_id VARCHAR(100),
    share_batch_id UUID,
    shared_by_user_id UUID,
    shared_at TIMESTAMPTZ,
    content_type VARCHAR(32) NOT NULL,
    content_ref VARCHAR(512) NOT NULL,
    original_content_ref VARCHAR(512),
    content_hash CHAR(64) NOT NULL,
    content_version INTEGER NOT NULL DEFAULT 1,
    content_visibility VARCHAR(16) NOT NULL DEFAULT 'original',
    original_access_required BOOLEAN NOT NULL DEFAULT FALSE,
    security_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    sensitivity VARCHAR(32),
    security_policy_version VARCHAR(64),
    content_saved BOOLEAN NOT NULL DEFAULT FALSE,
    ownership_ready BOOLEAN NOT NULL DEFAULT FALSE,
    security_ready BOOLEAN NOT NULL DEFAULT FALSE,
    permission_ready BOOLEAN NOT NULL DEFAULT FALSE,
    acl_version BIGINT NOT NULL DEFAULT 0,
    acl_sync_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    processing_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active',
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT knowledge_items_scope_chk
        CHECK (knowledge_scope IN ('private', 'organization')),
    CONSTRAINT knowledge_items_access_scope_chk
        CHECK (access_scope IN (
            'owner_only', 'organization_members', 'conversation_members'
        )),
    CONSTRAINT knowledge_items_owner_chk
        CHECK (
            (knowledge_scope = 'private'
                AND owner_user_id IS NOT NULL
                AND organization_id IS NULL
                AND access_scope = 'owner_only')
            OR
            (knowledge_scope = 'organization'
                AND owner_user_id IS NULL
                AND organization_id IS NOT NULL
                AND access_scope IN ('organization_members', 'conversation_members'))
        ),
    CONSTRAINT knowledge_items_source_type_chk
        CHECK (source_type IN (
            'platform_conversation',
            'private_conversation',
            'local_upload',
            'shared_private_item'
        )),
    CONSTRAINT knowledge_items_source_fields_chk
        CHECK (
            (source_type = 'platform_conversation'
                AND conversation_ingestion_id IS NOT NULL
                AND knowledge_scope = 'organization'
                AND access_scope = 'conversation_members')
            OR
            (source_type = 'private_conversation'
                AND source_message_id IS NOT NULL
                AND knowledge_scope = 'private')
            OR
            (source_type = 'local_upload'
                AND source_attachment_id IS NOT NULL)
            OR
            (source_type = 'shared_private_item'
                AND source_private_item_id IS NOT NULL
                AND source_private_item_id <> id
                AND share_request_id IS NOT NULL
                AND share_batch_id IS NOT NULL
                AND shared_by_user_id IS NOT NULL
                AND shared_at IS NOT NULL
                AND knowledge_scope = 'organization'
                AND access_scope = 'organization_members')
        ),
    CONSTRAINT knowledge_items_security_not_required_chk
        CHECK (
            source_type = 'platform_conversation'
            OR (security_status = 'not_required' AND security_ready)
        ),
    CONSTRAINT knowledge_items_content_type_chk
        CHECK (content_type IN ('text', 'file', 'image', 'spreadsheet', 'mixed')),
    CONSTRAINT knowledge_items_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT knowledge_items_visibility_chk
        CHECK (content_visibility IN ('original', 'masked', 'metadata_only')),
    CONSTRAINT knowledge_items_original_access_chk
        CHECK (NOT original_access_required OR original_content_ref IS NOT NULL),
    CONSTRAINT knowledge_items_security_status_chk
        CHECK (security_status IN (
            'not_required', 'pending', 'processing', 'classified', 'failed'
        )),
    CONSTRAINT knowledge_items_sensitivity_chk
        CHECK (
            sensitivity IS NULL
            OR sensitivity IN ('public', 'internal', 'confidential', 'restricted')
        ),
    CONSTRAINT knowledge_items_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT knowledge_items_acl_status_chk
        CHECK (acl_sync_status IN ('not_required', 'pending', 'synced', 'failed')),
    CONSTRAINT knowledge_items_processing_status_chk
        CHECK (processing_status IN ('pending', 'published', 'processing', 'ready', 'failed')),
    CONSTRAINT knowledge_items_lifecycle_chk CHECK (lifecycle_status = 'active')
);

CREATE UNIQUE INDEX knowledge_items_share_request_uq
    ON knowledge.knowledge_items (share_request_id, source_private_item_id)
    WHERE share_request_id IS NOT NULL AND source_private_item_id IS NOT NULL;
CREATE INDEX knowledge_items_base_idx ON knowledge.knowledge_items (knowledge_base_id);
CREATE INDEX knowledge_items_scope_idx ON knowledge.knowledge_items (knowledge_scope);
CREATE INDEX knowledge_items_access_scope_idx ON knowledge.knowledge_items (access_scope);
CREATE INDEX knowledge_items_owner_idx ON knowledge.knowledge_items (owner_user_id);
CREATE INDEX knowledge_items_org_idx ON knowledge.knowledge_items (organization_id);
CREATE INDEX knowledge_items_ingestion_idx
    ON knowledge.knowledge_items (conversation_ingestion_id);
CREATE INDEX knowledge_items_source_type_idx ON knowledge.knowledge_items (source_type);
CREATE INDEX knowledge_items_source_message_idx
    ON knowledge.knowledge_items (source_message_id);
CREATE INDEX knowledge_items_source_attachment_idx
    ON knowledge.knowledge_items (source_attachment_id);
CREATE INDEX knowledge_items_source_private_idx
    ON knowledge.knowledge_items (source_private_item_id);
CREATE INDEX knowledge_items_share_request_idx
    ON knowledge.knowledge_items (share_request_id);
CREATE INDEX knowledge_items_share_batch_idx
    ON knowledge.knowledge_items (share_batch_id);
CREATE INDEX knowledge_items_shared_by_idx
    ON knowledge.knowledge_items (shared_by_user_id);
CREATE INDEX knowledge_items_content_hash_idx ON knowledge.knowledge_items (content_hash);
CREATE INDEX knowledge_items_original_required_idx
    ON knowledge.knowledge_items (original_access_required);
CREATE INDEX knowledge_items_security_status_idx
    ON knowledge.knowledge_items (security_status);
CREATE INDEX knowledge_items_sensitivity_idx ON knowledge.knowledge_items (sensitivity);
CREATE INDEX knowledge_items_acl_status_idx
    ON knowledge.knowledge_items (acl_sync_status);
CREATE INDEX knowledge_items_processing_status_idx
    ON knowledge.knowledge_items (processing_status);
CREATE INDEX knowledge_items_lifecycle_idx
    ON knowledge.knowledge_items (lifecycle_status);
CREATE INDEX knowledge_items_created_at_idx ON knowledge.knowledge_items (created_at);

CREATE TABLE knowledge.outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    event_version BIGINT NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1,
    organization_id UUID,
    trace_id VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT outbox_events_aggregate_event_uq
        UNIQUE (aggregate_type, aggregate_id, event_type, event_version),
    CONSTRAINT outbox_events_event_version_chk CHECK (event_version >= 1),
    CONSTRAINT outbox_events_schema_version_chk CHECK (schema_version >= 1),
    CONSTRAINT outbox_events_status_chk
        CHECK (status IN ('pending', 'publishing', 'published', 'failed')),
    CONSTRAINT outbox_events_retry_count_chk CHECK (retry_count >= 0)
);

CREATE INDEX outbox_events_aggregate_idx
    ON knowledge.outbox_events (aggregate_type, aggregate_id);
CREATE INDEX outbox_events_type_idx ON knowledge.outbox_events (event_type);
CREATE INDEX outbox_events_org_idx ON knowledge.outbox_events (organization_id);
CREATE INDEX outbox_events_trace_idx ON knowledge.outbox_events (trace_id);
CREATE INDEX outbox_events_publish_idx
    ON knowledge.outbox_events (status, available_at);
CREATE INDEX outbox_events_created_at_idx ON knowledge.outbox_events (created_at);

-- RAG schema. IAM and Knowledge identifiers are logical references by design.

CREATE TABLE rag.processing_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_event_id UUID NOT NULL UNIQUE,
    event_type VARCHAR(100) NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    organization_id UUID,
    knowledge_item_id UUID NOT NULL,
    content_version INTEGER NOT NULL,
    acl_version BIGINT NOT NULL,
    job_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    current_stage VARCHAR(32),
    parser_name VARCHAR(64),
    parser_version VARCHAR(64),
    parsed_artifact_ref VARCHAR(512),
    parsed_content_hash CHAR(64),
    page_count INTEGER,
    chunking_version VARCHAR(64),
    embedding_model VARCHAR(128),
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT processing_jobs_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT processing_jobs_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT processing_jobs_type_chk
        CHECK (job_type IN (
            'preparse', 'full_process', 'reindex', 'acl_refresh', 'delete_index'
        )),
    CONSTRAINT processing_jobs_status_chk
        CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'dead')),
    CONSTRAINT processing_jobs_stage_chk
        CHECK (
            current_stage IS NULL
            OR current_stage IN ('fetch', 'parse', 'chunk', 'embed', 'index')
        ),
    CONSTRAINT processing_jobs_page_count_chk
        CHECK (page_count IS NULL OR page_count >= 0),
    CONSTRAINT processing_jobs_retry_count_chk CHECK (retry_count >= 0)
);

CREATE UNIQUE INDEX processing_jobs_unfinished_uq
    ON rag.processing_jobs (knowledge_item_id, content_version, job_type)
    WHERE status IN ('pending', 'processing', 'failed');
CREATE INDEX processing_jobs_event_type_idx ON rag.processing_jobs (event_type);
CREATE INDEX processing_jobs_org_idx ON rag.processing_jobs (organization_id);
CREATE INDEX processing_jobs_item_version_idx
    ON rag.processing_jobs (knowledge_item_id, content_version);
CREATE INDEX processing_jobs_acl_version_idx ON rag.processing_jobs (acl_version);
CREATE INDEX processing_jobs_job_type_idx ON rag.processing_jobs (job_type);
CREATE INDEX processing_jobs_run_idx ON rag.processing_jobs (status, available_at);
CREATE INDEX processing_jobs_stage_idx ON rag.processing_jobs (current_stage);
CREATE INDEX processing_jobs_created_at_idx ON rag.processing_jobs (created_at);

CREATE TABLE rag.index_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_item_id UUID NOT NULL,
    organization_id UUID,
    owner_user_id UUID,
    content_version INTEGER NOT NULL,
    acl_version BIGINT NOT NULL,
    content_variant VARCHAR(16) NOT NULL DEFAULT 'display',
    es_index_alias VARCHAR(255) NOT NULL DEFAULT 'knowledge_chunks_read',
    es_document_prefix VARCHAR(255) NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    mapping_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    indexed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT index_records_item_version_variant_uq
        UNIQUE (knowledge_item_id, content_version, content_variant),
    CONSTRAINT index_records_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT index_records_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT index_records_variant_chk CHECK (content_variant = 'display'),
    CONSTRAINT index_records_chunk_count_chk CHECK (chunk_count >= 0),
    CONSTRAINT index_records_status_chk
        CHECK (status IN ('pending', 'indexing', 'ready', 'failed', 'deleted')),
    CONSTRAINT index_records_owner_chk
        CHECK (NOT (organization_id IS NOT NULL AND owner_user_id IS NOT NULL))
);

CREATE INDEX index_records_org_idx ON rag.index_records (organization_id);
CREATE INDEX index_records_owner_idx ON rag.index_records (owner_user_id);
CREATE INDEX index_records_acl_version_idx ON rag.index_records (acl_version);
CREATE INDEX index_records_alias_idx ON rag.index_records (es_index_alias);
CREATE INDEX index_records_prefix_idx ON rag.index_records (es_document_prefix);
CREATE INDEX index_records_mapping_idx ON rag.index_records (mapping_version);
CREATE INDEX index_records_status_idx ON rag.index_records (status);
CREATE INDEX index_records_indexed_at_idx ON rag.index_records (indexed_at);

CREATE TABLE rag.search_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    organization_id UUID,
    query_text TEXT NOT NULL,
    query_hash CHAR(64) NOT NULL,
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    request_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT search_history_result_count_chk CHECK (result_count >= 0),
    CONSTRAINT search_history_duration_chk CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX search_history_user_created_idx
    ON rag.search_history (user_id, created_at DESC);
CREATE INDEX search_history_org_idx ON rag.search_history (organization_id);
CREATE INDEX search_history_query_hash_idx ON rag.search_history (query_hash);
CREATE INDEX search_history_request_idx ON rag.search_history (request_id);

CREATE TABLE rag.qa_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    organization_id UUID,
    title VARCHAR(300),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT qa_conversations_status_chk
        CHECK (status IN ('active', 'archived', 'deleted'))
);

CREATE INDEX qa_conversations_user_created_idx
    ON rag.qa_conversations (user_id, created_at DESC);
CREATE INDEX qa_conversations_org_idx ON rag.qa_conversations (organization_id);
CREATE INDEX qa_conversations_status_idx ON rag.qa_conversations (status);
CREATE INDEX qa_conversations_deleted_at_idx ON rag.qa_conversations (deleted_at);

CREATE TABLE rag.qa_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES rag.qa_conversations (id),
    parent_message_id UUID REFERENCES rag.qa_messages (id),
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    citations JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_name VARCHAR(128),
    prompt_version VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'completed',
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms INTEGER,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT qa_messages_parent_chk
        CHECK (parent_message_id IS NULL OR parent_message_id <> id),
    CONSTRAINT qa_messages_role_chk CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT qa_messages_citations_array_chk
        CHECK (jsonb_typeof(citations) = 'array'),
    CONSTRAINT qa_messages_status_chk
        CHECK (status IN ('streaming', 'completed', 'failed', 'cancelled')),
    CONSTRAINT qa_messages_duration_chk CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX qa_messages_conversation_created_idx
    ON rag.qa_messages (conversation_id, created_at);
CREATE INDEX qa_messages_parent_idx ON rag.qa_messages (parent_message_id);
CREATE INDEX qa_messages_model_idx ON rag.qa_messages (model_name);
CREATE INDEX qa_messages_prompt_version_idx ON rag.qa_messages (prompt_version);
CREATE INDEX qa_messages_status_idx ON rag.qa_messages (status);

COMMIT;
