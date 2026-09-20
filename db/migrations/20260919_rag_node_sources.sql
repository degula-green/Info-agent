BEGIN;

CREATE TABLE IF NOT EXISTS rag.memory_node_sources (
    node_id UUID NOT NULL REFERENCES rag.memory_nodes(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES rag.memory_sources(id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL DEFAULT 'reference',
    position INTEGER,
    relevance_score NUMERIC(5,4),
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_locator JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_version INTEGER NOT NULL CHECK (content_version >= 1),
    mount_version BIGINT NOT NULL DEFAULT 1 CHECK (mount_version >= 1),
    mount_strategy_version VARCHAR(64),
    visibility VARCHAR(16),
    access_scope VARCHAR(32),
    sensitivity VARCHAR(32),
    auth_object_key VARCHAR(512),
    acl_version BIGINT NOT NULL DEFAULT 0 CHECK (acl_version >= 0),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (node_id, source_id),
    CONSTRAINT memory_node_sources_relation_chk
        CHECK (relation_type IN ('primary','evidence','reference','policy')),
    CONSTRAINT memory_node_sources_position_chk
        CHECK (position IS NULL OR position >= 0),
    CONSTRAINT memory_node_sources_relevance_chk
        CHECK (relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 1)),
    CONSTRAINT memory_node_sources_metadata_chk
        CHECK (jsonb_typeof(source_metadata) = 'object'),
    CONSTRAINT memory_node_sources_locator_chk
        CHECK (jsonb_typeof(source_locator) = 'object'),
    CONSTRAINT memory_node_sources_status_chk
        CHECK (status IN ('active','removed'))
);

CREATE INDEX IF NOT EXISTS memory_node_sources_node_active_idx
    ON rag.memory_node_sources (node_id, relation_type, position)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS memory_node_sources_source_active_idx
    ON rag.memory_node_sources (source_id, node_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS memory_node_sources_auth_active_idx
    ON rag.memory_node_sources (auth_object_key, acl_version)
    WHERE status = 'active' AND auth_object_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS memory_node_sources_version_idx
    ON rag.memory_node_sources (node_id, content_version, mount_version);

COMMIT;
