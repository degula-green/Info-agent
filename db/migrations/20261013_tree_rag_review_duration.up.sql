-- Dwell time for a candidate review.
--
-- Approval/rejection rates are derivable from the action column, but "how long
-- did a human spend on this candidate" is only observable at the moment of the
-- review and only the review page knows it. Without the column the first real
-- reviews would lose that measurement permanently, which is why it lands before
-- any review exists rather than after.
--
-- NULL means "not reported": a scripted or API-driven review has no dwell time,
-- and that is different from "took zero milliseconds".
BEGIN;

ALTER TABLE rag_mvp.entity_review_requests
    ADD COLUMN IF NOT EXISTS review_duration_ms INTEGER;

-- The client is the only source for this value, so bound it at the storage
-- layer too (the API clamps as well): a clock skew must not write a 3-day
-- "review" that skews the average.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'entity_review_requests_duration_chk'
    ) THEN
        ALTER TABLE rag_mvp.entity_review_requests
            ADD CONSTRAINT entity_review_requests_duration_chk
            CHECK (
                review_duration_ms IS NULL
                OR (review_duration_ms >= 0 AND review_duration_ms <= 86400000)
            );
    END IF;
END $$;

COMMIT;
