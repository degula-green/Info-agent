BEGIN;

ALTER TABLE rag_mvp.entity_review_requests
    DROP CONSTRAINT IF EXISTS entity_review_requests_duration_chk;

ALTER TABLE rag_mvp.entity_review_requests
    DROP COLUMN IF EXISTS review_duration_ms;

COMMIT;
