BEGIN;

CREATE SCHEMA IF NOT EXISTS rag_mvp;

CREATE TABLE IF NOT EXISTS rag_mvp.processing_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_event_id UUID NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    job_type VARCHAR(32) NOT NULL DEFAULT 'full_process',
    knowledge_item_id UUID NOT NULL,
    resource_type VARCHAR(16) NOT NULL,
    resource_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    source_conversation_id UUID,
    source_audience_policy VARCHAR(32),
    content_version INTEGER NOT NULL,
    processing_version VARCHAR(128) NOT NULL,
    acl_version BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    current_stage VARCHAR(32),
    lease_owner VARCHAR(128),
    lease_until TIMESTAMPTZ,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT processing_jobs_payload_hash_chk CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT processing_jobs_job_type_chk CHECK (job_type IN ('full_process', 'reindex')),
    CONSTRAINT processing_jobs_resource_type_chk CHECK (resource_type IN ('message', 'attachment')),
    CONSTRAINT processing_jobs_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT processing_jobs_audience_policy_chk CHECK (
        source_audience_policy IS NULL
        OR source_audience_policy IN ('source_conversation_members', 'organization_members', 'owner_only')
    ),
    CONSTRAINT processing_jobs_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT processing_jobs_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT processing_jobs_status_chk CHECK (
        status IN ('pending', 'processing', 'ready', 'metadata_only', 'failed', 'cancelled')
    ),
    CONSTRAINT processing_jobs_stage_chk CHECK (
        current_stage IS NULL
        OR current_stage IN ('fetch', 'parse', 'chunk', 'embed', 'index', 'memory', 'callback')
    ),
    CONSTRAINT processing_jobs_retry_count_chk CHECK (retry_count >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_source_event_uq
    ON rag_mvp.processing_jobs (source_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_active_full_process_uq
    ON rag_mvp.processing_jobs (knowledge_item_id, resource_type, resource_id, content_version)
    WHERE job_type = 'full_process' AND status IN ('pending', 'processing');
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_active_reindex_uq
    ON rag_mvp.processing_jobs (knowledge_item_id, resource_type, resource_id, content_version, processing_version)
    WHERE job_type = 'reindex' AND status IN ('pending', 'processing');
CREATE INDEX IF NOT EXISTS processing_jobs_dispatch_idx
    ON rag_mvp.processing_jobs (status, next_retry_at, lease_until);
CREATE INDEX IF NOT EXISTS processing_jobs_resource_idx
    ON rag_mvp.processing_jobs (knowledge_item_id, resource_type, resource_id, content_version);
CREATE INDEX IF NOT EXISTS processing_jobs_scope_kb_idx
    ON rag_mvp.processing_jobs (scope_type, scope_id, knowledge_base_id);
CREATE INDEX IF NOT EXISTS processing_jobs_source_conversation_idx
    ON rag_mvp.processing_jobs (source_conversation_id);
CREATE INDEX IF NOT EXISTS processing_jobs_stage_idx
    ON rag_mvp.processing_jobs (current_stage);
CREATE INDEX IF NOT EXISTS processing_jobs_created_at_idx
    ON rag_mvp.processing_jobs (created_at);

CREATE TABLE IF NOT EXISTS rag_mvp.processing_job_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES rag_mvp.processing_jobs(id) ON DELETE CASCADE,
    lane VARCHAR(32) NOT NULL,
    stage VARCHAR(32) NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    error_code VARCHAR(64),
    error_message TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT processing_job_attempts_attempt_chk CHECK (attempt >= 1),
    CONSTRAINT processing_job_attempts_status_chk CHECK (status IN ('running', 'succeeded', 'failed')),
    CONSTRAINT processing_job_attempts_metrics_object_chk CHECK (jsonb_typeof(metrics) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS processing_job_attempts_stage_attempt_uq
    ON rag_mvp.processing_job_attempts (job_id, stage, attempt);
CREATE INDEX IF NOT EXISTS processing_job_attempts_job_created_idx
    ON rag_mvp.processing_job_attempts (job_id, created_at);
CREATE INDEX IF NOT EXISTS processing_job_attempts_status_created_idx
    ON rag_mvp.processing_job_attempts (status, created_at);

CREATE TABLE IF NOT EXISTS rag_mvp.outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES rag_mvp.processing_jobs(id) ON DELETE SET NULL,
    aggregate_type VARCHAR(32) NOT NULL DEFAULT 'knowledge_item',
    aggregate_id UUID NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    event_version BIGINT NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    trace_id VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT outbox_events_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT outbox_events_event_version_chk CHECK (event_version >= 1),
    CONSTRAINT outbox_events_schema_version_chk CHECK (schema_version >= 1),
    CONSTRAINT outbox_events_status_chk CHECK (status IN ('pending', 'publishing', 'published', 'failed')),
    CONSTRAINT outbox_events_retry_count_chk CHECK (retry_count >= 0),
    CONSTRAINT outbox_events_payload_object_chk CHECK (jsonb_typeof(payload) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS outbox_events_aggregate_event_uq
    ON rag_mvp.outbox_events (aggregate_type, aggregate_id, event_type, event_version);
CREATE INDEX IF NOT EXISTS outbox_events_publish_idx
    ON rag_mvp.outbox_events (status, available_at);
CREATE INDEX IF NOT EXISTS outbox_events_job_idx
    ON rag_mvp.outbox_events (job_id);
CREATE INDEX IF NOT EXISTS outbox_events_scope_idx
    ON rag_mvp.outbox_events (scope_type, scope_id, created_at);
CREATE INDEX IF NOT EXISTS outbox_events_trace_idx
    ON rag_mvp.outbox_events (trace_id);
CREATE INDEX IF NOT EXISTS outbox_events_created_at_idx
    ON rag_mvp.outbox_events (created_at);

CREATE TABLE IF NOT EXISTS rag_mvp.resource_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_item_id UUID NOT NULL,
    resource_type VARCHAR(16) NOT NULL,
    resource_id UUID NOT NULL,
    resource_ref TEXT NOT NULL,
    knowledge_base_id UUID NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    source_conversation_id UUID,
    source_conversation_type VARCHAR(16),
    source_audience_policy VARCHAR(32),
    external_conversation_id VARCHAR(255),
    storage_backend_id VARCHAR(36),
    object_ref TEXT,
    sender_identity_id UUID,
    sender_platform VARCHAR(32),
    sender_workspace_key VARCHAR(255) NOT NULL DEFAULT '',
    sender_external_user_id VARCHAR(255),
    sender_mapped_user_id UUID,
    sender_display_name VARCHAR(255),
    content_version INTEGER NOT NULL DEFAULT 1,
    content_hash CHAR(64) NOT NULL,
    acl_version BIGINT NOT NULL DEFAULT 0,
    access_scope VARCHAR(32),
    sensitivity VARCHAR(32),
    has_display_content BOOLEAN NOT NULL DEFAULT FALSE,
    has_protected_content BOOLEAN NOT NULL DEFAULT FALSE,
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT resource_snapshots_resource_type_chk CHECK (resource_type IN ('message', 'attachment')),
    CONSTRAINT resource_snapshots_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT resource_snapshots_conversation_type_chk CHECK (
        source_conversation_type IS NULL OR source_conversation_type IN ('group', 'private')
    ),
    CONSTRAINT resource_snapshots_audience_policy_chk CHECK (
        source_audience_policy IS NULL
        OR source_audience_policy IN ('source_conversation_members', 'organization_members', 'owner_only')
    ),
    CONSTRAINT resource_snapshots_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT resource_snapshots_content_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT resource_snapshots_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT resource_snapshots_lifecycle_chk CHECK (lifecycle_status IN ('active', 'inactive', 'deleted'))
);

CREATE UNIQUE INDEX IF NOT EXISTS resource_snapshots_item_version_uq
    ON rag_mvp.resource_snapshots (knowledge_item_id, content_version);
CREATE INDEX IF NOT EXISTS resource_snapshots_scope_kb_idx
    ON rag_mvp.resource_snapshots (scope_type, scope_id, knowledge_base_id, lifecycle_status);
CREATE INDEX IF NOT EXISTS resource_snapshots_conversation_idx
    ON rag_mvp.resource_snapshots (source_conversation_id, content_version);
CREATE INDEX IF NOT EXISTS resource_snapshots_resource_ref_idx
    ON rag_mvp.resource_snapshots (resource_ref);
CREATE INDEX IF NOT EXISTS resource_snapshots_content_hash_idx
    ON rag_mvp.resource_snapshots (content_hash);
CREATE INDEX IF NOT EXISTS resource_snapshots_sender_identity_idx
    ON rag_mvp.resource_snapshots (sender_identity_id);
CREATE INDEX IF NOT EXISTS resource_snapshots_sender_external_idx
    ON rag_mvp.resource_snapshots (sender_platform, sender_workspace_key, sender_external_user_id);
CREATE INDEX IF NOT EXISTS resource_snapshots_sender_mapped_user_idx
    ON rag_mvp.resource_snapshots (sender_mapped_user_id);

CREATE TABLE IF NOT EXISTS rag_mvp.entity_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    domain VARCHAR(32) NOT NULL,
    canonical_name VARCHAR(512) NOT NULL,
    normalized_key VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    merged_into_entity_id UUID REFERENCES rag_mvp.entity_registry(id) ON DELETE SET NULL,
    registry_version BIGINT NOT NULL DEFAULT 1,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_registry_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_registry_domain_chk CHECK (
        domain IN ('organization', 'project', 'person', 'policy', 'contract', 'asset', 'location', 'unclassified')
    ),
    CONSTRAINT entity_registry_status_chk CHECK (status IN ('active', 'disabled', 'merged')),
    CONSTRAINT entity_registry_version_chk CHECK (registry_version >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_registry_key_uq
    ON rag_mvp.entity_registry (scope_type, scope_id, domain, normalized_key);
CREATE INDEX IF NOT EXISTS entity_registry_scope_domain_status_idx
    ON rag_mvp.entity_registry (scope_type, scope_id, domain, status);
CREATE INDEX IF NOT EXISTS entity_registry_merged_into_idx
    ON rag_mvp.entity_registry (merged_into_entity_id);
CREATE INDEX IF NOT EXISTS entity_registry_version_idx
    ON rag_mvp.entity_registry (registry_version);

CREATE TABLE IF NOT EXISTS rag_mvp.entity_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    domain VARCHAR(32) NOT NULL,
    display_alias VARCHAR(512) NOT NULL,
    normalized_alias VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    source VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_aliases_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_aliases_status_chk CHECK (status IN ('active', 'disabled'))
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_aliases_key_uq
    ON rag_mvp.entity_aliases (scope_type, scope_id, domain, normalized_alias);
CREATE INDEX IF NOT EXISTS entity_aliases_entity_status_idx
    ON rag_mvp.entity_aliases (entity_id, status);

CREATE TABLE IF NOT EXISTS rag_mvp.entity_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    candidate_name VARCHAR(512) NOT NULL,
    normalized_key VARCHAR(512) NOT NULL,
    candidate_domain VARCHAR(32) NOT NULL DEFAULT 'unclassified',
    mention_count INTEGER NOT NULL DEFAULT 0,
    distinct_chunk_count INTEGER NOT NULL DEFAULT 0,
    distinct_source_count INTEGER NOT NULL DEFAULT 0,
    distinct_conversation_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sample_context TEXT,
    suggested_entity_id UUID REFERENCES rag_mvp.entity_registry(id) ON DELETE SET NULL,
    score NUMERIC(5, 4) NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    resolved_entity_id UUID REFERENCES rag_mvp.entity_registry(id) ON DELETE SET NULL,
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_candidates_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_candidates_domain_chk CHECK (
        candidate_domain IN ('organization', 'project', 'person', 'policy', 'contract', 'asset', 'location', 'unclassified')
    ),
    CONSTRAINT entity_candidates_status_chk CHECK (
        status IN ('new', 'grouped', 'review_ready', 'merged', 'promoted', 'ignored', 'deferred')
    ),
    CONSTRAINT entity_candidates_counts_chk CHECK (
        mention_count >= 0
        AND distinct_chunk_count >= 0
        AND distinct_source_count >= 0
        AND distinct_conversation_count >= 0
    ),
    CONSTRAINT entity_candidates_score_chk CHECK (score >= 0 AND score <= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_candidates_key_uq
    ON rag_mvp.entity_candidates (scope_type, scope_id, candidate_domain, normalized_key);
CREATE INDEX IF NOT EXISTS entity_candidates_review_idx
    ON rag_mvp.entity_candidates (scope_type, scope_id, status, score DESC);
CREATE INDEX IF NOT EXISTS entity_candidates_suggested_idx
    ON rag_mvp.entity_candidates (suggested_entity_id);
CREATE INDEX IF NOT EXISTS entity_candidates_resolved_idx
    ON rag_mvp.entity_candidates (resolved_entity_id);
CREATE INDEX IF NOT EXISTS entity_candidates_reviewed_idx
    ON rag_mvp.entity_candidates (reviewed_by, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS entity_candidates_last_seen_idx
    ON rag_mvp.entity_candidates (last_seen_at);

CREATE TABLE IF NOT EXISTS rag_mvp.chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id CHAR(64) NOT NULL,
    resource_snapshot_id UUID NOT NULL REFERENCES rag_mvp.resource_snapshots(id) ON DELETE CASCADE,
    knowledge_item_id UUID NOT NULL,
    resource_type VARCHAR(16) NOT NULL,
    resource_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    source_conversation_id UUID,
    conversation_type VARCHAR(16),
    document_id UUID,
    message_id UUID,
    content_version INTEGER NOT NULL,
    processing_version VARCHAR(128) NOT NULL,
    chunking_version VARCHAR(64) NOT NULL,
    content_variant VARCHAR(16) NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    title VARCHAR(512),
    file_name VARCHAR(512),
    heading_path TEXT[] NOT NULL DEFAULT '{}',
    context_header JSONB NOT NULL DEFAULT '{}'::jsonb,
    content TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_locator JSONB NOT NULL DEFAULT '{}'::jsonb,
    sent_at TIMESTAMPTZ,
    auth_partition_key TEXT,
    auth_object_key TEXT,
    acl_version BIGINT NOT NULL DEFAULT 0,
    sensitivity VARCHAR(32),
    embedding_model VARCHAR(128),
    embedding_dimensions INTEGER,
    embedding_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    rag_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chunks_chunk_id_chk CHECK (chunk_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chunks_resource_type_chk CHECK (resource_type IN ('message', 'attachment')),
    CONSTRAINT chunks_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT chunks_conversation_type_chk CHECK (conversation_type IS NULL OR conversation_type IN ('group', 'private')),
    CONSTRAINT chunks_content_version_chk CHECK (content_version >= 1),
    CONSTRAINT chunks_content_variant_chk CHECK (content_variant IN ('display', 'protected')),
    CONSTRAINT chunks_index_count_chk CHECK (chunk_index >= 0 AND chunk_count >= 1),
    CONSTRAINT chunks_content_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chunks_acl_version_chk CHECK (acl_version >= 0),
    CONSTRAINT chunks_embedding_status_chk CHECK (embedding_status IN ('pending', 'ready', 'failed')),
    CONSTRAINT chunks_lifecycle_status_chk CHECK (lifecycle_status IN ('active', 'inactive', 'deleted')),
    CONSTRAINT chunks_heading_path_array_chk CHECK (array_ndims(heading_path) IS NULL OR array_ndims(heading_path) = 1),
    CONSTRAINT chunks_context_header_object_chk CHECK (jsonb_typeof(context_header) = 'object'),
    CONSTRAINT chunks_source_locator_object_chk CHECK (jsonb_typeof(source_locator) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS chunks_chunk_id_uq
    ON rag_mvp.chunks (chunk_id);
CREATE UNIQUE INDEX IF NOT EXISTS chunks_position_uq
    ON rag_mvp.chunks (
        knowledge_item_id,
        content_version,
        processing_version,
        content_variant,
        chunk_index
    );
CREATE INDEX IF NOT EXISTS chunks_snapshot_order_idx
    ON rag_mvp.chunks (resource_snapshot_id, chunk_index);
CREATE INDEX IF NOT EXISTS chunks_scope_kb_lifecycle_idx
    ON rag_mvp.chunks (scope_type, scope_id, knowledge_base_id, lifecycle_status);
CREATE INDEX IF NOT EXISTS chunks_conversation_sent_idx
    ON rag_mvp.chunks (source_conversation_id, sent_at);
CREATE INDEX IF NOT EXISTS chunks_variant_embedding_idx
    ON rag_mvp.chunks (content_variant, embedding_status);
CREATE INDEX IF NOT EXISTS chunks_document_idx
    ON rag_mvp.chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_message_idx
    ON rag_mvp.chunks (message_id);
CREATE INDEX IF NOT EXISTS chunks_heading_path_gin_idx
    ON rag_mvp.chunks USING GIN (heading_path);
CREATE INDEX IF NOT EXISTS chunks_content_hash_idx
    ON rag_mvp.chunks (content_hash);
CREATE INDEX IF NOT EXISTS chunks_auth_partition_idx
    ON rag_mvp.chunks (auth_partition_key);
CREATE INDEX IF NOT EXISTS chunks_auth_object_idx
    ON rag_mvp.chunks (auth_object_key);

CREATE TABLE IF NOT EXISTS rag_mvp.tree_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    domain VARCHAR(32) NOT NULL,
    entity_id UUID REFERENCES rag_mvp.entity_registry(id) ON DELETE SET NULL,
    time_bucket VARCHAR(7),
    parent_id UUID REFERENCES rag_mvp.tree_nodes(id) ON DELETE CASCADE,
    node_type VARCHAR(16) NOT NULL,
    node_key VARCHAR(512) NOT NULL,
    branch_key VARCHAR(512),
    registry_version BIGINT NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    statistics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT tree_nodes_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT tree_nodes_domain_chk CHECK (
        domain IN ('organization', 'project', 'person', 'policy', 'contract', 'asset', 'location', 'unclassified')
    ),
    CONSTRAINT tree_nodes_time_bucket_chk CHECK (time_bucket IS NULL OR time_bucket ~ '^\d{4}-\d{2}$'),
    CONSTRAINT tree_nodes_node_type_chk CHECK (node_type IN ('scope', 'domain', 'entity', 'time')),
    CONSTRAINT tree_nodes_registry_version_chk CHECK (registry_version >= 1),
    CONSTRAINT tree_nodes_status_chk CHECK (status IN ('active', 'archived')),
    CONSTRAINT tree_nodes_statistics_object_chk CHECK (jsonb_typeof(statistics) = 'object'),
    CONSTRAINT tree_nodes_scope_branch_key_uq UNIQUE (scope_type, scope_id, branch_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS tree_nodes_scope_node_key_uq
    ON rag_mvp.tree_nodes (scope_type, scope_id, node_key);
CREATE INDEX IF NOT EXISTS tree_nodes_parent_idx
    ON rag_mvp.tree_nodes (parent_id, node_type);
CREATE INDEX IF NOT EXISTS tree_nodes_entity_time_idx
    ON rag_mvp.tree_nodes (entity_id, time_bucket);
CREATE INDEX IF NOT EXISTS tree_nodes_scope_domain_status_idx
    ON rag_mvp.tree_nodes (scope_type, scope_id, domain, status);
CREATE INDEX IF NOT EXISTS tree_nodes_registry_version_idx
    ON rag_mvp.tree_nodes (registry_version);

CREATE TABLE IF NOT EXISTS rag_mvp.entity_candidate_mentions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id UUID NOT NULL REFERENCES rag_mvp.entity_candidates(id) ON DELETE CASCADE,
    chunk_id CHAR(64) NOT NULL REFERENCES rag_mvp.chunks(chunk_id) ON DELETE CASCADE,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    surface_form VARCHAR(512) NOT NULL,
    candidate_domain VARCHAR(32) NOT NULL DEFAULT 'unclassified',
    context_excerpt TEXT,
    confidence NUMERIC(5, 4) NOT NULL DEFAULT 0,
    extraction_method VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_candidate_mentions_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_candidate_mentions_confidence_chk CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT entity_candidate_mentions_method_chk CHECK (extraction_method IN ('regex', 'metadata', 'llm', 'alias'))
);

CREATE INDEX IF NOT EXISTS entity_candidate_mentions_candidate_created_idx
    ON rag_mvp.entity_candidate_mentions (candidate_id, created_at);
CREATE INDEX IF NOT EXISTS entity_candidate_mentions_chunk_idx
    ON rag_mvp.entity_candidate_mentions (chunk_id);
CREATE UNIQUE INDEX IF NOT EXISTS entity_candidate_mentions_dedupe_uq
    ON rag_mvp.entity_candidate_mentions (candidate_id, chunk_id, surface_form, extraction_method);
CREATE INDEX IF NOT EXISTS entity_candidate_mentions_method_idx
    ON rag_mvp.entity_candidate_mentions (extraction_method);

CREATE TABLE IF NOT EXISTS rag_mvp.entity_review_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id UUID NOT NULL REFERENCES rag_mvp.entity_candidates(id) ON DELETE CASCADE,
    review_request_id UUID NOT NULL,
    action VARCHAR(16) NOT NULL,
    target_entity_id UUID REFERENCES rag_mvp.entity_registry(id) ON DELETE SET NULL,
    reviewer_id UUID NOT NULL,
    expected_status VARCHAR(16),
    result_status VARCHAR(16) NOT NULL,
    registry_version BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_review_requests_action_chk CHECK (
        action IN ('promote', 'merge', 'ignore', 'defer')
    ),
    CONSTRAINT entity_review_requests_result_status_chk CHECK (
        result_status IN ('merged', 'promoted', 'ignored', 'deferred')
    ),
    CONSTRAINT entity_review_requests_registry_version_chk CHECK (
        registry_version IS NULL OR registry_version >= 1
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_review_requests_request_uq
    ON rag_mvp.entity_review_requests (candidate_id, review_request_id);
CREATE INDEX IF NOT EXISTS entity_review_requests_target_idx
    ON rag_mvp.entity_review_requests (target_entity_id);
CREATE INDEX IF NOT EXISTS entity_review_requests_reviewer_idx
    ON rag_mvp.entity_review_requests (reviewer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS rag_mvp.chunk_branches (
    chunk_id CHAR(64) NOT NULL REFERENCES rag_mvp.chunks(chunk_id) ON DELETE CASCADE,
    branch_key VARCHAR(512) NOT NULL,
    entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    registry_version BIGINT NOT NULL,
    match_method VARCHAR(32) NOT NULL DEFAULT 'exact',
    match_score NUMERIC(5, 4) NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id, branch_key),
    CONSTRAINT chunk_branches_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT chunk_branches_registry_version_chk CHECK (registry_version >= 1),
    CONSTRAINT chunk_branches_method_chk CHECK (match_method IN ('exact', 'alias', 'confirmed')),
    CONSTRAINT chunk_branches_score_chk CHECK (match_score >= 0 AND match_score <= 1),
    CONSTRAINT chunk_branches_status_chk CHECK (status IN ('active', 'removed')),
    CONSTRAINT chunk_branches_tree_node_fk
        FOREIGN KEY (scope_type, scope_id, branch_key)
        REFERENCES rag_mvp.tree_nodes (scope_type, scope_id, branch_key)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS chunk_branches_branch_status_idx
    ON rag_mvp.chunk_branches (branch_key, status);
CREATE INDEX IF NOT EXISTS chunk_branches_entity_version_status_idx
    ON rag_mvp.chunk_branches (entity_id, registry_version, status);

CREATE TABLE IF NOT EXISTS rag_mvp.branch_refresh_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    registry_version BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    target_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT branch_refresh_jobs_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT branch_refresh_jobs_registry_version_chk CHECK (registry_version >= 1),
    CONSTRAINT branch_refresh_jobs_status_chk CHECK (status IN ('pending', 'processing', 'succeeded', 'failed')),
    CONSTRAINT branch_refresh_jobs_counts_chk CHECK (
        target_count >= 0 AND processed_count >= 0 AND retry_count >= 0
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS branch_refresh_jobs_active_uq
    ON rag_mvp.branch_refresh_jobs (entity_id, registry_version)
    WHERE status IN ('pending', 'processing');
CREATE INDEX IF NOT EXISTS branch_refresh_jobs_dispatch_idx
    ON rag_mvp.branch_refresh_jobs (status, next_retry_at);

CREATE TABLE IF NOT EXISTS rag_mvp.projection_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id CHAR(64) NOT NULL REFERENCES rag_mvp.chunks(chunk_id) ON DELETE CASCADE,
    chunk_variant VARCHAR(16) NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    knowledge_base_id UUID NOT NULL,
    es_index_alias VARCHAR(255) NOT NULL,
    es_document_id VARCHAR(255) NOT NULL,
    mapping_version VARCHAR(32) NOT NULL,
    embedding_model VARCHAR(128),
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    indexed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT projection_records_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT projection_records_variant_chk CHECK (chunk_variant IN ('display', 'protected')),
    CONSTRAINT projection_records_status_chk CHECK (status IN ('pending', 'indexing', 'ready', 'failed', 'deleted'))
);

CREATE UNIQUE INDEX IF NOT EXISTS projection_records_chunk_mapping_uq
    ON rag_mvp.projection_records (chunk_id, chunk_variant, mapping_version);
CREATE INDEX IF NOT EXISTS projection_records_retry_idx
    ON rag_mvp.projection_records (status, es_index_alias);
CREATE INDEX IF NOT EXISTS projection_records_scope_kb_idx
    ON rag_mvp.projection_records (scope_type, scope_id, knowledge_base_id);

CREATE TABLE IF NOT EXISTS rag_mvp.search_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    query_hash CHAR(64) NOT NULL,
    query_redacted TEXT,
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
    tree_mode VARCHAR(16),
    execution_path VARCHAR(32),
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    request_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT search_history_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT search_history_query_hash_chk CHECK (query_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT search_history_tree_mode_chk CHECK (tree_mode IS NULL OR tree_mode IN ('off', 'shadow', 'boost')),
    CONSTRAINT search_history_result_count_chk CHECK (result_count >= 0),
    CONSTRAINT search_history_duration_chk CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT search_history_filters_object_chk CHECK (jsonb_typeof(filters) = 'object'),
    CONSTRAINT search_history_diagnostics_object_chk CHECK (jsonb_typeof(diagnostics) = 'object')
);

CREATE INDEX IF NOT EXISTS search_history_user_created_idx
    ON rag_mvp.search_history (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS search_history_scope_created_idx
    ON rag_mvp.search_history (scope_type, scope_id, created_at DESC);
CREATE INDEX IF NOT EXISTS search_history_tree_mode_created_idx
    ON rag_mvp.search_history (tree_mode, created_at DESC);
CREATE INDEX IF NOT EXISTS search_history_query_hash_idx
    ON rag_mvp.search_history (query_hash);
CREATE INDEX IF NOT EXISTS search_history_request_idx
    ON rag_mvp.search_history (request_id);

CREATE TABLE IF NOT EXISTS rag_mvp.qa_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    title VARCHAR(300),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    retrieval_mode VARCHAR(16) NOT NULL DEFAULT 'quick',
    knowledge_base_ids UUID[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT qa_conversations_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT qa_conversations_status_chk CHECK (status IN ('active', 'archived', 'deleted')),
    CONSTRAINT qa_conversations_retrieval_mode_chk CHECK (retrieval_mode IN ('quick', 'deep'))
);

CREATE INDEX IF NOT EXISTS qa_conversations_active_updated_idx
    ON rag_mvp.qa_conversations (user_id, updated_at DESC)
    WHERE status = 'active' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS qa_conversations_scope_updated_idx
    ON rag_mvp.qa_conversations (scope_type, scope_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS qa_conversations_status_idx
    ON rag_mvp.qa_conversations (status);
CREATE INDEX IF NOT EXISTS qa_conversations_deleted_at_idx
    ON rag_mvp.qa_conversations (deleted_at);
CREATE INDEX IF NOT EXISTS qa_conversations_kb_ids_gin_idx
    ON rag_mvp.qa_conversations USING GIN (knowledge_base_ids);

CREATE TABLE IF NOT EXISTS rag_mvp.qa_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES rag_mvp.qa_conversations(id) ON DELETE CASCADE,
    parent_message_id UUID REFERENCES rag_mvp.qa_messages(id) ON DELETE SET NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    citations JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_name VARCHAR(128),
    prompt_version VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'completed',
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms INTEGER,
    error_code VARCHAR(64),
    error_stage VARCHAR(32),
    error_class VARCHAR(128),
    error_message_safe TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    diagnostic_ref VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT qa_messages_role_chk CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT qa_messages_status_chk CHECK (status IN ('streaming', 'completed', 'failed', 'cancelled')),
    CONSTRAINT qa_messages_citations_array_chk CHECK (jsonb_typeof(citations) = 'array'),
    CONSTRAINT qa_messages_token_usage_object_chk CHECK (jsonb_typeof(token_usage) = 'object'),
    CONSTRAINT qa_messages_duration_chk CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX IF NOT EXISTS qa_messages_conversation_created_idx
    ON rag_mvp.qa_messages (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS qa_messages_parent_idx
    ON rag_mvp.qa_messages (parent_message_id);
CREATE INDEX IF NOT EXISTS qa_messages_model_idx
    ON rag_mvp.qa_messages (model_name);
CREATE INDEX IF NOT EXISTS qa_messages_prompt_version_idx
    ON rag_mvp.qa_messages (prompt_version);
CREATE INDEX IF NOT EXISTS qa_messages_status_idx
    ON rag_mvp.qa_messages (status);
CREATE INDEX IF NOT EXISTS qa_messages_diagnostic_ref_idx
    ON rag_mvp.qa_messages (diagnostic_ref);

COMMIT;
