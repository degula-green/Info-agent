-- Window-scan progress for the Phase 2 mount worker.
--
-- Progress is tracked per conversation as a watermark rather than a boolean
-- flag on chunks: a boolean only says "seen", so a failed batch either loses
-- work (if marked) or repeats forever (if not). A watermark advances only
-- after a window is applied successfully, which makes the scan retryable and
-- resumable.
BEGIN;

CREATE TABLE IF NOT EXISTS rag_mvp.entity_scan_watermarks (
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    conversation_id UUID NOT NULL,
    last_sent_at TIMESTAMPTZ NOT NULL,
    last_chunk_id CHAR(64),
    window_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope_type, scope_id, conversation_id),
    CONSTRAINT entity_scan_watermarks_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_scan_watermarks_window_count_chk CHECK (window_count >= 0)
);

CREATE INDEX IF NOT EXISTS entity_scan_watermarks_scope_idx
    ON rag_mvp.entity_scan_watermarks (scope_type, scope_id, last_sent_at);

COMMIT;
