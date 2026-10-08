-- Labelled evaluation sets for the tree RAG layers.
--
-- A labelled set has to be frozen to be comparable: the same query keeps
-- pointing at the same gold answer while prompts, models and thresholds change,
-- otherwise a metric movement cannot be attributed to anything. Cases therefore
-- carry a dataset_version and are retired rather than edited in place.
BEGIN;

CREATE TABLE IF NOT EXISTS rag_mvp.eval_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    suite VARCHAR(32) NOT NULL,
    dataset_version INTEGER NOT NULL DEFAULT 1,
    query TEXT NOT NULL,
    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT eval_cases_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT eval_cases_suite_chk CHECK (
        suite IN ('entity_location', 'retrieval', 'mount')
    ),
    CONSTRAINT eval_cases_status_chk CHECK (status IN ('active', 'retired')),
    CONSTRAINT eval_cases_version_chk CHECK (dataset_version >= 1),
    CONSTRAINT eval_cases_labels_object_chk CHECK (jsonb_typeof(labels) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS eval_cases_key_uq
    ON rag_mvp.eval_cases (scope_type, scope_id, suite, dataset_version, query);
CREATE INDEX IF NOT EXISTS eval_cases_suite_idx
    ON rag_mvp.eval_cases (scope_type, scope_id, suite, dataset_version, status);

CREATE TABLE IF NOT EXISTS rag_mvp.eval_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    suite VARCHAR(32) NOT NULL,
    dataset_version INTEGER NOT NULL,
    case_count INTEGER NOT NULL DEFAULT 0,
    passed_count INTEGER NOT NULL DEFAULT 0,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    label VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT eval_runs_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT eval_runs_suite_chk CHECK (
        suite IN ('entity_location', 'retrieval', 'mount')
    ),
    CONSTRAINT eval_runs_counts_chk CHECK (case_count >= 0 AND passed_count >= 0),
    CONSTRAINT eval_runs_metrics_object_chk CHECK (jsonb_typeof(metrics) = 'object')
);

CREATE INDEX IF NOT EXISTS eval_runs_lookup_idx
    ON rag_mvp.eval_runs (scope_type, scope_id, suite, created_at DESC);

COMMIT;
