BEGIN;

ALTER TABLE rag_mvp.processing_jobs
    ADD COLUMN IF NOT EXISTS parse_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0;

ALTER TABLE rag_mvp.processing_jobs
    DROP CONSTRAINT IF EXISTS processing_jobs_status_chk;
ALTER TABLE rag_mvp.processing_jobs
    ADD CONSTRAINT processing_jobs_status_chk CHECK (
        status IN (
            'pending',
            'processing',
            'retry_wait',
            'ready',
            'metadata_only',
            'failed',
            'cancelled'
        )
    );

ALTER TABLE rag_mvp.processing_jobs
    DROP CONSTRAINT IF EXISTS processing_jobs_parse_status_chk;
ALTER TABLE rag_mvp.processing_jobs
    ADD CONSTRAINT processing_jobs_parse_status_chk CHECK (
        parse_status IN ('pending', 'parsed', 'metadata_only', 'failed')
    );

ALTER TABLE rag_mvp.processing_jobs
    DROP CONSTRAINT IF EXISTS processing_jobs_lease_epoch_chk;
ALTER TABLE rag_mvp.processing_jobs
    ADD CONSTRAINT processing_jobs_lease_epoch_chk CHECK (lease_epoch >= 0);

DROP INDEX IF EXISTS rag_mvp.processing_jobs_active_full_process_uq;
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_active_full_process_uq
    ON rag_mvp.processing_jobs (
        knowledge_item_id,
        resource_type,
        resource_id,
        content_version
    )
    WHERE job_type = 'full_process'
      AND status IN ('pending', 'processing', 'retry_wait');

DROP INDEX IF EXISTS rag_mvp.processing_jobs_active_reindex_uq;
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_active_reindex_uq
    ON rag_mvp.processing_jobs (
        knowledge_item_id,
        resource_type,
        resource_id,
        content_version,
        processing_version
    )
    WHERE job_type = 'reindex'
      AND status IN ('pending', 'processing', 'retry_wait');

UPDATE rag_mvp.processing_jobs AS j
SET parse_status = CASE
    WHEN EXISTS (
        SELECT 1
        FROM rag_mvp.chunks AS c
        WHERE c.knowledge_item_id = j.knowledge_item_id
          AND c.content_version = j.content_version
    ) THEN 'parsed'
    WHEN j.status = 'metadata_only' THEN 'metadata_only'
    WHEN j.status = 'failed' THEN 'failed'
    ELSE 'pending'
END;

ALTER TABLE rag_mvp.projection_records
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS failure_stage VARCHAR(16);

ALTER TABLE rag_mvp.projection_records
    DROP CONSTRAINT IF EXISTS projection_records_status_chk;
ALTER TABLE rag_mvp.projection_records
    ADD CONSTRAINT projection_records_status_chk CHECK (
        status IN (
            'pending',
            'indexing',
            'ready',
            'retry_wait',
            'failed',
            'deleted'
        )
    );

ALTER TABLE rag_mvp.projection_records
    DROP CONSTRAINT IF EXISTS projection_records_retry_count_chk;
ALTER TABLE rag_mvp.projection_records
    ADD CONSTRAINT projection_records_retry_count_chk CHECK (retry_count >= 0);

ALTER TABLE rag_mvp.projection_records
    DROP CONSTRAINT IF EXISTS projection_records_failure_stage_chk;
ALTER TABLE rag_mvp.projection_records
    ADD CONSTRAINT projection_records_failure_stage_chk CHECK (
        failure_stage IS NULL
        OR failure_stage IN ('embedding', 'indexing')
    );

DROP INDEX IF EXISTS rag_mvp.projection_records_chunk_mapping_uq;
CREATE UNIQUE INDEX IF NOT EXISTS projection_records_chunk_mapping_uq
    ON rag_mvp.projection_records (
        chunk_id,
        es_index_alias,
        mapping_version
    );

DROP INDEX IF EXISTS rag_mvp.projection_records_retry_idx;
CREATE INDEX IF NOT EXISTS projection_records_retry_idx
    ON rag_mvp.projection_records (
        status,
        es_index_alias,
        next_retry_at
    );

COMMIT;
