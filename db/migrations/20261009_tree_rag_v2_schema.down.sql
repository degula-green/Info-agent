-- Reverts the structure only. Dropped columns, dropped tree_nodes rows and
-- cleared tree-side data are NOT restored: they are derived from chunks and are
-- rebuilt by the mount backfill, not by this script.
BEGIN;

DROP TABLE IF EXISTS rag_mvp.entity_relations;

DROP INDEX IF EXISTS rag_mvp.chunk_branches_entity_method_idx;

ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_pkey;
ALTER TABLE rag_mvp.chunk_branches
    ADD COLUMN IF NOT EXISTS branch_key VARCHAR(512);
ALTER TABLE rag_mvp.chunk_branches
    ADD PRIMARY KEY (chunk_id, branch_key);
CREATE INDEX IF NOT EXISTS chunk_branches_branch_status_idx
    ON rag_mvp.chunk_branches (branch_key, status);

ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_confidence_chk;
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_mount_method_chk;
ALTER TABLE rag_mvp.chunk_branches DROP COLUMN IF EXISTS confidence;
ALTER TABLE rag_mvp.chunk_branches DROP COLUMN IF EXISTS mount_method;

ALTER TABLE rag_mvp.chunk_branches
    ADD COLUMN IF NOT EXISTS match_method VARCHAR(32) NOT NULL DEFAULT 'exact',
    ADD COLUMN IF NOT EXISTS match_score NUMERIC(5,4) NOT NULL DEFAULT 1;
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_method_chk;
ALTER TABLE rag_mvp.chunk_branches
    ADD CONSTRAINT chunk_branches_method_chk CHECK (
        match_method IN ('exact', 'alias', 'confirmed')
    );
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_score_chk;
ALTER TABLE rag_mvp.chunk_branches
    ADD CONSTRAINT chunk_branches_score_chk CHECK (
        match_score >= 0 AND match_score <= 1
    );

-- tree_nodes returns as an empty shell; its rows were derived data.
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
        domain IN (
            'organization', 'project', 'person', 'policy', 'contract',
            'asset', 'location', 'unclassified'
        )
    ),
    CONSTRAINT tree_nodes_time_bucket_chk CHECK (
        time_bucket IS NULL OR time_bucket ~ '^\d{4}-\d{2}$'
    ),
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

ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_tree_node_fk;
ALTER TABLE rag_mvp.chunk_branches
    ADD CONSTRAINT chunk_branches_tree_node_fk
        FOREIGN KEY (scope_type, scope_id, branch_key)
        REFERENCES rag_mvp.tree_nodes (scope_type, scope_id, branch_key)
        DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE rag_mvp.entity_aliases
    DROP CONSTRAINT IF EXISTS entity_aliases_domain_chk;

ALTER TABLE rag_mvp.entity_candidates
    DROP CONSTRAINT IF EXISTS entity_candidates_domain_chk;
ALTER TABLE rag_mvp.entity_candidates
    ADD CONSTRAINT entity_candidates_domain_chk CHECK (
        candidate_domain IN (
            'organization', 'project', 'person', 'policy', 'contract',
            'asset', 'location', 'unclassified'
        )
    );

ALTER TABLE rag_mvp.entity_registry
    DROP CONSTRAINT IF EXISTS entity_registry_embedding_status_chk;

DROP INDEX IF EXISTS rag_mvp.entity_registry_embedding_hnsw_idx;
DROP INDEX IF EXISTS rag_mvp.entity_registry_name_trgm_idx;
DROP INDEX IF EXISTS rag_mvp.entity_registry_normalized_trgm_idx;
DROP INDEX IF EXISTS rag_mvp.entity_registry_keywords_gin_idx;

ALTER TABLE rag_mvp.entity_registry
    DROP COLUMN IF EXISTS keywords,
    DROP COLUMN IF EXISTS description,
    DROP COLUMN IF EXISTS embedding,
    DROP COLUMN IF EXISTS embedding_model,
    DROP COLUMN IF EXISTS embedding_dimensions,
    DROP COLUMN IF EXISTS embedding_status,
    DROP COLUMN IF EXISTS embedding_updated_at,
    DROP COLUMN IF EXISTS chunk_count,
    DROP COLUMN IF EXISTS last_mentioned_at;

ALTER TABLE rag_mvp.entity_registry
    DROP CONSTRAINT IF EXISTS entity_registry_domain_chk;
ALTER TABLE rag_mvp.entity_registry
    ADD CONSTRAINT entity_registry_domain_chk CHECK (
        domain IN (
            'organization', 'project', 'person', 'policy', 'contract',
            'asset', 'location', 'unclassified'
        )
    );

COMMIT;
