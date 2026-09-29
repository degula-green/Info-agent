BEGIN;

CREATE TABLE IF NOT EXISTS rag_mvp.entity_review_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id UUID NOT NULL REFERENCES rag_mvp.entity_candidates(id) ON DELETE CASCADE,
    review_request_id UUID NOT NULL,
    action VARCHAR(16) NOT NULL,
    target_entity_id UUID REFERENCES rag_mvp.entity_registry(id) ON DELETE SET NULL,
    reviewer_id UUID NOT NULL,
    expected_status VARCHAR(16),
    result_status VARCHAR(16) NOT NULL,
    registry_version BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_review_requests_action_chk CHECK (
        action IN ('promote', 'merge', 'ignore', 'defer')
    ),
    CONSTRAINT entity_review_requests_result_status_chk CHECK (
        result_status IN ('merged', 'promoted', 'ignored', 'deferred')
    ),
    CONSTRAINT entity_review_requests_registry_version_chk CHECK (
        registry_version IS NULL OR registry_version >= 1
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_review_requests_request_uq
    ON rag_mvp.entity_review_requests (candidate_id, review_request_id);
CREATE INDEX IF NOT EXISTS entity_review_requests_target_idx
    ON rag_mvp.entity_review_requests (target_entity_id);
CREATE INDEX IF NOT EXISTS entity_review_requests_reviewer_idx
    ON rag_mvp.entity_review_requests (reviewer_id, created_at DESC);

COMMIT;
