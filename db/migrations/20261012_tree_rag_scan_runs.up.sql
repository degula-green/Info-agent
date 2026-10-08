-- One row per window-scan sweep, per scope.
--
-- The scan is the only place that can observe how often the extraction model
-- returned nothing for a window. Measured on real windows the rate was 43.75%
-- before the prompt fix and 18.75% after, and a residual ~1/8 of windows still
-- swing between runs. Without this table that number is invisible in
-- production, and mount coverage alone cannot distinguish "no entities in this
-- conversation" from "the model skipped the window".
BEGIN;

CREATE TABLE IF NOT EXISTS rag_mvp.entity_scan_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    conversations INTEGER NOT NULL DEFAULT 0,
    windows INTEGER NOT NULL DEFAULT 0,
    empty_windows INTEGER NOT NULL DEFAULT 0,
    mounts INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    relations INTEGER NOT NULL DEFAULT 0,
    failed_conversations INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_scan_runs_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_scan_runs_counts_chk CHECK (
        conversations >= 0 AND windows >= 0 AND empty_windows >= 0
        AND mounts >= 0 AND candidates >= 0 AND relations >= 0
        AND failed_conversations >= 0
    ),
    CONSTRAINT entity_scan_runs_empty_chk CHECK (empty_windows <= windows)
);

CREATE INDEX IF NOT EXISTS entity_scan_runs_scope_created_idx
    ON rag_mvp.entity_scan_runs (scope_type, scope_id, created_at DESC);

COMMIT;
