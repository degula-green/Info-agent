BEGIN;

ALTER TABLE rag.processing_jobs DROP CONSTRAINT IF EXISTS processing_jobs_stage_chk;
ALTER TABLE rag.processing_jobs ADD CONSTRAINT processing_jobs_stage_chk CHECK (
    current_stage IS NULL OR current_stage IN (
        'fetch','parse','chunk','embed','index','fact','tree','summary','memory_index'
    )
);

ALTER TABLE rag.index_records ADD COLUMN IF NOT EXISTS object_type VARCHAR(16) NOT NULL DEFAULT 'chunk';
ALTER TABLE rag.index_records ADD COLUMN IF NOT EXISTS object_id UUID;
ALTER TABLE rag.index_records ADD COLUMN IF NOT EXISTS es_document_id VARCHAR(255);
ALTER TABLE rag.index_records ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128);
ALTER TABLE rag.index_records DROP CONSTRAINT IF EXISTS index_records_item_version_variant_uq;
DROP INDEX IF EXISTS rag.index_records_chunk_projection_uq;
DROP INDEX IF EXISTS rag.index_records_memory_projection_uq;
CREATE UNIQUE INDEX index_records_chunk_projection_uq
    ON rag.index_records (knowledge_item_id, content_version, content_variant)
    WHERE object_type = 'chunk';
CREATE UNIQUE INDEX index_records_memory_projection_uq
    ON rag.index_records (object_type, object_id, es_document_id, content_variant, content_version, mapping_version)
    WHERE object_type IN ('fact','node');
ALTER TABLE rag.index_records DROP CONSTRAINT IF EXISTS index_records_object_type_chk;
ALTER TABLE rag.index_records ADD CONSTRAINT index_records_object_type_chk
    CHECK (object_type IN ('chunk','fact','node'));

CREATE TABLE IF NOT EXISTS rag.memory_sources (
    id UUID PRIMARY KEY,
    source_type VARCHAR(32) NOT NULL,
    source_resource_id VARCHAR(255) NOT NULL,
    knowledge_item_id UUID NOT NULL,
    message_id UUID,
    attachment_id UUID,
    conversation_key VARCHAR(255),
    knowledge_base_id UUID NOT NULL,
    organization_id UUID,
    owner_user_id UUID,
    source_ref VARCHAR(512),
    content_version INTEGER NOT NULL CHECK (content_version >= 1),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    acl_version BIGINT NOT NULL DEFAULT 0 CHECK (acl_version >= 0),
    visibility VARCHAR(16),
    access_scope VARCHAR(32),
    sensitivity VARCHAR(32),
    auth_object_key VARCHAR(512),
    processing_version VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_sources_scope_chk CHECK (NOT (organization_id IS NOT NULL AND owner_user_id IS NOT NULL)),
    CONSTRAINT memory_sources_type_chk CHECK (source_type IN ('message','attachment','knowledge_item')),
    CONSTRAINT memory_sources_version_uq UNIQUE (source_type, source_resource_id, content_version, processing_version)
);
CREATE INDEX IF NOT EXISTS memory_sources_item_version_idx ON rag.memory_sources (knowledge_item_id, content_version);
CREATE INDEX IF NOT EXISTS memory_sources_attachment_version_idx ON rag.memory_sources (attachment_id, content_version);
CREATE INDEX IF NOT EXISTS memory_sources_auth_idx ON rag.memory_sources (auth_object_key);

CREATE TABLE IF NOT EXISTS rag.memory_chunks (
    id UUID PRIMARY KEY,
    external_chunk_id VARCHAR(128) NOT NULL UNIQUE,
    source_id UUID NOT NULL REFERENCES rag.memory_sources(id) ON DELETE CASCADE,
    knowledge_item_id UUID NOT NULL,
    attachment_id UUID,
    content_version INTEGER NOT NULL CHECK (content_version >= 1),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 1),
    chunk_text TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    chunking_version VARCHAR(64) NOT NULL,
    source_locator JSONB NOT NULL DEFAULT '{}'::jsonb,
    visibility VARCHAR(16),
    access_scope VARCHAR(32),
    sensitivity VARCHAR(32),
    auth_object_key VARCHAR(512),
    acl_version BIGINT NOT NULL DEFAULT 0 CHECK (acl_version >= 0),
    embedding_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_chunks_source_order_uq UNIQUE (source_id, chunking_version, chunk_index),
    CONSTRAINT memory_chunks_embedding_status_chk CHECK (embedding_status IN ('pending','ready','failed'))
);
CREATE INDEX IF NOT EXISTS memory_chunks_item_version_idx ON rag.memory_chunks (knowledge_item_id, content_version);
CREATE INDEX IF NOT EXISTS memory_chunks_source_idx ON rag.memory_chunks (source_id, chunk_index);

CREATE TABLE IF NOT EXISTS rag.memory_entities (
    id UUID PRIMARY KEY,
    knowledge_base_id UUID NOT NULL,
    organization_id UUID,
    owner_user_id UUID,
    entity_type VARCHAR(64) NOT NULL,
    canonical_name VARCHAR(512) NOT NULL,
    normalized_key VARCHAR(512) NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    merge_status VARCHAR(32) NOT NULL DEFAULT 'active',
    confidence NUMERIC(5,4),
    recognition_version VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_entities_scope_chk CHECK (NOT (organization_id IS NOT NULL AND owner_user_id IS NOT NULL)),
    CONSTRAINT memory_entities_key_uq UNIQUE (knowledge_base_id, entity_type, normalized_key)
);

CREATE TABLE IF NOT EXISTS rag.memory_facts (
    id UUID PRIMARY KEY,
    knowledge_base_id UUID NOT NULL,
    organization_id UUID,
    owner_user_id UUID,
    fact_type VARCHAR(32) NOT NULL,
    dedupe_key CHAR(64) NOT NULL,
    subject_entity_id UUID REFERENCES rag.memory_entities(id),
    predicate VARCHAR(256),
    current_version_id UUID,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    visibility VARCHAR(16),
    access_scope VARCHAR(32),
    sensitivity VARCHAR(32),
    auth_object_key VARCHAR(512),
    acl_version BIGINT NOT NULL DEFAULT 0 CHECK (acl_version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_facts_scope_chk CHECK (NOT (organization_id IS NOT NULL AND owner_user_id IS NOT NULL)),
    CONSTRAINT memory_facts_type_chk CHECK (fact_type IN ('event','state','task','relation')),
    CONSTRAINT memory_facts_status_chk CHECK (status IN ('active','superseded','revoked','historical','conflict')),
    CONSTRAINT memory_facts_dedupe_uq UNIQUE (knowledge_base_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS rag.memory_fact_versions (
    id UUID PRIMARY KEY,
    fact_id UUID NOT NULL REFERENCES rag.memory_facts(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    fact_text TEXT NOT NULL,
    normalized_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    supersedes_version_id UUID REFERENCES rag.memory_fact_versions(id),
    revoke_reason TEXT,
    confidence NUMERIC(5,4),
    extraction_version VARCHAR(64),
    content_version INTEGER NOT NULL CHECK (content_version >= 1),
    visibility VARCHAR(16),
    access_scope VARCHAR(32),
    sensitivity VARCHAR(32),
    auth_object_key VARCHAR(512),
    acl_version BIGINT NOT NULL DEFAULT 0 CHECK (acl_version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_fact_versions_status_chk CHECK (status IN ('active','superseded','revoked','historical','conflict')),
    CONSTRAINT memory_fact_versions_no_uq UNIQUE (fact_id, version_no)
);
ALTER TABLE rag.memory_facts DROP CONSTRAINT IF EXISTS memory_facts_current_version_fk;
ALTER TABLE rag.memory_facts ADD CONSTRAINT memory_facts_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES rag.memory_fact_versions(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS rag.memory_fact_chunks (
    fact_id UUID NOT NULL REFERENCES rag.memory_facts(id) ON DELETE CASCADE,
    fact_version_id UUID NOT NULL REFERENCES rag.memory_fact_versions(id) ON DELETE CASCADE,
    chunk_id UUID NOT NULL REFERENCES rag.memory_chunks(id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL DEFAULT 'evidence',
    evidence_text TEXT,
    confidence NUMERIC(5,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fact_version_id, chunk_id),
    CONSTRAINT memory_fact_chunks_relation_chk CHECK (relation_type IN ('evidence','support','contradiction'))
);

CREATE TABLE IF NOT EXISTS rag.memory_trees (
    id UUID PRIMARY KEY,
    knowledge_base_id UUID NOT NULL,
    organization_id UUID,
    owner_user_id UUID,
    tree_type VARCHAR(16) NOT NULL,
    subject_key VARCHAR(512) NOT NULL,
    root_node_id UUID,
    strategy_version VARCHAR(64) NOT NULL,
    current_version BIGINT NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_trees_scope_chk CHECK (NOT (organization_id IS NOT NULL AND owner_user_id IS NOT NULL)),
    CONSTRAINT memory_trees_type_chk CHECK (tree_type IN ('session','entity','scene')),
    CONSTRAINT memory_trees_status_chk CHECK (status IN ('active','paused','archived')),
    CONSTRAINT memory_trees_subject_uq UNIQUE (knowledge_base_id, tree_type, subject_key)
);

CREATE TABLE IF NOT EXISTS rag.memory_nodes (
    id UUID PRIMARY KEY,
    tree_id UUID NOT NULL REFERENCES rag.memory_trees(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES rag.memory_nodes(id) ON DELETE CASCADE,
    node_type VARCHAR(16) NOT NULL,
    level INTEGER NOT NULL CHECK (level >= 0),
    node_key VARCHAR(512) NOT NULL,
    topic_key VARCHAR(256),
    phase_key VARCHAR(256),
    time_start TIMESTAMPTZ,
    time_end TIMESTAMPTZ,
    display_summary TEXT,
    protected_summary TEXT,
    summary_version INTEGER NOT NULL DEFAULT 0,
    summary_strategy_version VARCHAR(64),
    content_version BIGINT NOT NULL DEFAULT 1,
    dirty BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_nodes_type_chk CHECK (node_type IN ('root','internal','leaf')),
    CONSTRAINT memory_nodes_status_chk CHECK (status IN ('active','archived')),
    CONSTRAINT memory_nodes_key_uq UNIQUE (tree_id, node_key)
);
CREATE INDEX IF NOT EXISTS memory_nodes_parent_idx ON rag.memory_nodes (tree_id, parent_id, level);
CREATE INDEX IF NOT EXISTS memory_nodes_type_idx ON rag.memory_nodes (tree_id, node_type);
ALTER TABLE rag.memory_trees DROP CONSTRAINT IF EXISTS memory_trees_root_fk;
ALTER TABLE rag.memory_trees ADD CONSTRAINT memory_trees_root_fk
    FOREIGN KEY (root_node_id) REFERENCES rag.memory_nodes(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS rag.memory_node_facts (
    node_id UUID NOT NULL REFERENCES rag.memory_nodes(id) ON DELETE CASCADE,
    fact_id UUID NOT NULL REFERENCES rag.memory_facts(id) ON DELETE CASCADE,
    fact_version_id UUID NOT NULL REFERENCES rag.memory_fact_versions(id) ON DELETE CASCADE,
    position INTEGER,
    relation_type VARCHAR(32) NOT NULL DEFAULT 'primary',
    mount_strategy_version VARCHAR(64),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (node_id, fact_version_id),
    CONSTRAINT memory_node_facts_status_chk CHECK (status IN ('active','removed'))
);

COMMIT;
