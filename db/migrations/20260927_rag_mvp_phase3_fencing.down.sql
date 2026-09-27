BEGIN;

DROP INDEX IF EXISTS rag_mvp.processing_job_attempts_lease_idx;

ALTER TABLE rag_mvp.processing_job_attempts
    DROP CONSTRAINT IF EXISTS processing_job_attempts_lease_epoch_chk;

ALTER TABLE rag_mvp.processing_job_attempts
    DROP COLUMN IF EXISTS lease_epoch;
ALTER TABLE rag_mvp.processing_job_attempts
    DROP COLUMN IF EXISTS lease_owner;

COMMIT;
