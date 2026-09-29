BEGIN;

UPDATE rag_mvp.processing_jobs
SET status = 'processing'
WHERE status = 'retry_wait';

UPDATE rag_mvp.projection_records
SET status = 'pending'
WHERE status = 'retry_wait';

DROP INDEX IF EXISTS rag_mvp.projection_records_retry_idx;
CREATE INDEX IF NOT EXISTS projection_records_retry_idx
    ON rag_mvp.projection_records (status, es_index_alias);

DROP INDEX IF EXISTS rag_mvp.projection_records_chunk_mapping_uq;
CREATE UNIQUE INDEX IF NOT EXISTS projection_records_chunk_mapping_uq
    ON rag_mvp.projection_records (
        chunk_id,
        chunk_variant,
        mapping_version
    );

ALTER TABLE rag_mvp.projection_records
    DROP CONSTRAINT IF EXISTS projection_records_failure_stage_chk;
ALTER TABLE rag_mvp.projection_records
    DROP CONSTRAINT IF EXISTS projection_records_retry_count_chk;
ALTER TABLE rag_mvp.projection_records
    DROP CONSTRAINT IF EXISTS projection_records_status_chk;
ALTER TABLE rag_mvp.projection_records
    ADD CONSTRAINT projection_records_status_chk CHECK (
        status IN ('pending', 'indexing', 'ready', 'failed', 'deleted')
    );

ALTER TABLE rag_mvp.projection_records
    DROP COLUMN IF EXISTS failure_stage;
ALTER TABLE rag_mvp.projection_records
    DROP COLUMN IF EXISTS next_retry_at;
ALTER TABLE rag_mvp.projection_records
    DROP COLUMN IF EXISTS retry_count;

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
      AND status IN ('pending', 'processing');

DROP INDEX IF EXISTS rag_mvp.processing_jobs_active_full_process_uq;
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_active_full_process_uq
    ON rag_mvp.processing_jobs (
        knowledge_item_id,
        resource_type,
        resource_id,
        content_version
    )
    WHERE job_type = 'full_process'
      AND status IN ('pending', 'processing');

ALTER TABLE rag_mvp.processing_jobs
    DROP CONSTRAINT IF EXISTS processing_jobs_lease_epoch_chk;
ALTER TABLE rag_mvp.processing_jobs
    DROP CONSTRAINT IF EXISTS processing_jobs_parse_status_chk;
ALTER TABLE rag_mvp.processing_jobs
    DROP CONSTRAINT IF EXISTS processing_jobs_status_chk;
ALTER TABLE rag_mvp.processing_jobs
    ADD CONSTRAINT processing_jobs_status_chk CHECK (
        status IN (
            'pending',
            'processing',
            'ready',
            'metadata_only',
            'failed',
            'cancelled'
        )
    );

ALTER TABLE rag_mvp.processing_jobs
    DROP COLUMN IF EXISTS lease_epoch;
ALTER TABLE rag_mvp.processing_jobs
    DROP COLUMN IF EXISTS parse_status;

COMMIT;
