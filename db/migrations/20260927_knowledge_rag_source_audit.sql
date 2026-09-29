BEGIN;

CREATE TABLE IF NOT EXISTS knowledge.rag_source_access_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    caller_service VARCHAR(64) NOT NULL,
    purpose VARCHAR(32) NOT NULL DEFAULT '',
    knowledge_item_id UUID NOT NULL,
    resource_id UUID,
    content_version INTEGER NOT NULL,
    acl_version BIGINT NOT NULL DEFAULT 0,
    content_variant VARCHAR(16) NOT NULL,
    rag_job_id UUID,
    trace_id VARCHAR(128),
    result VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT rag_source_access_audit_result_chk CHECK (
        result IN ('success', 'denied', 'failed')
    ),
    CONSTRAINT rag_source_access_audit_variant_chk CHECK (
        content_variant IN ('display', 'original')
    ),
    CONSTRAINT rag_source_access_audit_version_chk CHECK (
        content_version >= 1 AND acl_version >= 0
    )
);

CREATE INDEX IF NOT EXISTS rag_source_access_audit_item_idx
    ON knowledge.rag_source_access_audit (knowledge_item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS rag_source_access_audit_job_idx
    ON knowledge.rag_source_access_audit (rag_job_id)
    WHERE rag_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS rag_source_access_audit_trace_idx
    ON knowledge.rag_source_access_audit (trace_id)
    WHERE trace_id IS NOT NULL;

COMMIT;
