BEGIN;

ALTER TABLE rag_mvp.processing_job_attempts
    ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128),
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT;

ALTER TABLE rag_mvp.processing_job_attempts
    DROP CONSTRAINT IF EXISTS processing_job_attempts_lease_epoch_chk;
ALTER TABLE rag_mvp.processing_job_attempts
    ADD CONSTRAINT processing_job_attempts_lease_epoch_chk CHECK (
        lease_epoch IS NULL OR lease_epoch >= 0
    );

CREATE INDEX IF NOT EXISTS processing_job_attempts_lease_idx
    ON rag_mvp.processing_job_attempts (job_id, lease_epoch, created_at);

COMMIT;
