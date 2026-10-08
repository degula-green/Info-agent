-- Tree RAG v2: reshape the existing tree tables in place instead of adding a
-- parallel set. The tree-side tables only hold data derived from chunks, so
-- this migration is destructive on purpose.
BEGIN;

-- 0) Extensions this migration depends on. pgvector must exist before any
--    vector column is declared, and gin_trgm_ops backs the fuzzy name indexes
--    below. Both are available on the server; this only enables them here.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1) entity_registry: narrow the domain set and add semantic/statistics columns.
--    Rows in the dropped domains (asset/location/unclassified) are removed
--    first: this release does not carry the old domain semantics forward, and
--    the CHECK below would otherwise reject them. Mounts and candidate
--    mentions cascade with their entity.
DELETE FROM rag_mvp.entity_aliases
 WHERE domain NOT IN ('organization', 'person', 'project', 'policy', 'contract');
DELETE FROM rag_mvp.entity_candidates
 WHERE candidate_domain NOT IN ('organization', 'person', 'project', 'policy', 'contract');
DELETE FROM rag_mvp.entity_registry
 WHERE domain NOT IN ('organization', 'person', 'project', 'policy', 'contract');

ALTER TABLE rag_mvp.entity_registry
    DROP CONSTRAINT IF EXISTS entity_registry_domain_chk;
ALTER TABLE rag_mvp.entity_registry
    ADD CONSTRAINT entity_registry_domain_chk CHECK (
        domain IN ('organization', 'person', 'project', 'policy', 'contract')
    );

ALTER TABLE rag_mvp.entity_registry
    ADD COLUMN IF NOT EXISTS keywords TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS embedding vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128),
    ADD COLUMN IF NOT EXISTS embedding_dimensions INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_mentioned_at TIMESTAMPTZ;

ALTER TABLE rag_mvp.entity_registry
    DROP CONSTRAINT IF EXISTS entity_registry_embedding_status_chk;
ALTER TABLE rag_mvp.entity_registry
    ADD CONSTRAINT entity_registry_embedding_status_chk CHECK (
        embedding_status IN ('pending', 'ready', 'failed')
    );

-- HNSW over IVFFlat: entities are inserted continuously and IVFFlat needs
-- training data plus a lists parameter before the index is useful.
CREATE INDEX IF NOT EXISTS entity_registry_embedding_hnsw_idx
    ON rag_mvp.entity_registry USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS entity_registry_name_trgm_idx
    ON rag_mvp.entity_registry USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entity_registry_normalized_trgm_idx
    ON rag_mvp.entity_registry USING gin (normalized_key gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entity_registry_keywords_gin_idx
    ON rag_mvp.entity_registry USING gin (keywords);

-- 2) Candidates and aliases follow the same five domains. entity_aliases had no
--    domain constraint before, so this is an addition rather than a narrowing.
ALTER TABLE rag_mvp.entity_candidates
    DROP CONSTRAINT IF EXISTS entity_candidates_domain_chk;
ALTER TABLE rag_mvp.entity_candidates
    ADD CONSTRAINT entity_candidates_domain_chk CHECK (
        candidate_domain IN ('organization', 'person', 'project', 'policy', 'contract')
    );

ALTER TABLE rag_mvp.entity_aliases
    DROP CONSTRAINT IF EXISTS entity_aliases_domain_chk;
ALTER TABLE rag_mvp.entity_aliases
    ADD CONSTRAINT entity_aliases_domain_chk CHECK (
        domain IN ('organization', 'person', 'project', 'policy', 'contract')
    );

-- 3) chunk_branches becomes the mount table: one row per (chunk, entity) with a
--    confidence and the channel it came from. match_method/match_score only
--    ever distinguished exact from alias, which confidence and mount_method
--    now express, so they go away rather than drift out of sync.
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_method_chk;
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_score_chk;

ALTER TABLE rag_mvp.chunk_branches
    ADD COLUMN IF NOT EXISTS confidence NUMERIC(5,4) NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS mount_method VARCHAR(32) NOT NULL DEFAULT 'explicit';

ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_mount_method_chk;
ALTER TABLE rag_mvp.chunk_branches
    ADD CONSTRAINT chunk_branches_mount_method_chk CHECK (
        mount_method IN ('explicit', 'window_batch', 'llm_infer')
    );
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_confidence_chk;
ALTER TABLE rag_mvp.chunk_branches
    ADD CONSTRAINT chunk_branches_confidence_chk CHECK (
        confidence >= 0 AND confidence <= 1
    );

ALTER TABLE rag_mvp.chunk_branches DROP COLUMN IF EXISTS match_method;
ALTER TABLE rag_mvp.chunk_branches DROP COLUMN IF EXISTS match_score;

-- 4) The materialised tree is gone, so branch_key and its foreign key go too.
--    branch_key is part of the current primary key, so the key has to be
--    dropped before the column can be.
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_tree_node_fk;
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_pkey;
ALTER TABLE rag_mvp.chunk_branches DROP COLUMN IF EXISTS branch_key;
ALTER TABLE rag_mvp.chunk_branches
    ADD PRIMARY KEY (chunk_id, entity_id);

CREATE INDEX IF NOT EXISTS chunk_branches_entity_method_idx
    ON rag_mvp.chunk_branches (entity_id, mount_method, confidence);

DROP TABLE IF EXISTS rag_mvp.tree_nodes;

-- 5) The graph side: directed, typed edges between entities.
CREATE TABLE IF NOT EXISTS rag_mvp.entity_relations (
    relation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    source_entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    target_entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL,
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0.8,
    evidence_chunk_ids CHAR(64)[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_relations_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_relations_type_chk CHECK (relation_type IN (
        'works_for', 'participates_in', 'belongs_to', 'governed_by',
        'signed_by', 'related_to', 'applies_to', 'contacts'
    )),
    CONSTRAINT entity_relations_confidence_chk CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_relations_key_uq
    ON rag_mvp.entity_relations (
        scope_type, scope_id, source_entity_id, target_entity_id, relation_type
    );
CREATE INDEX IF NOT EXISTS entity_relations_source_idx
    ON rag_mvp.entity_relations (scope_type, scope_id, source_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS entity_relations_target_idx
    ON rag_mvp.entity_relations (scope_type, scope_id, target_entity_id, relation_type);

COMMIT;
