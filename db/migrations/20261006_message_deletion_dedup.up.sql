BEGIN;

ALTER TABLE knowledge.deletion_requests
    DROP CONSTRAINT IF EXISTS deletion_requests_status_chk;
ALTER TABLE knowledge.deletion_requests
    ADD CONSTRAINT deletion_requests_status_chk
    CHECK (status IN ('pending', 'approved', 'rejected', 'executing', 'completed', 'failed', 'superseded'));

WITH active_requests AS (
    SELECT
        request.id,
        request.scope_type,
        request.scope_id,
        request.requested_at,
        COALESCE(message.delete_request_id, item.delete_request_id) AS preferred_id
    FROM knowledge.deletion_requests request
    LEFT JOIN knowledge.messages message
        ON request.scope_type = 'message' AND message.id = request.scope_id
    LEFT JOIN knowledge.knowledge_items item
        ON request.scope_type = 'knowledge_item' AND item.id = request.scope_id
    WHERE request.status IN ('pending', 'approved', 'executing')
),
ranked_requests AS (
    SELECT
        active.*,
        ROW_NUMBER() OVER (
            PARTITION BY active.scope_type, active.scope_id
            ORDER BY
                CASE WHEN active.id = active.preferred_id THEN 0 ELSE 1 END,
                active.requested_at ASC,
                active.id ASC
        ) AS canonical_rank,
        FIRST_VALUE(active.id) OVER (
            PARTITION BY active.scope_type, active.scope_id
            ORDER BY
                CASE WHEN active.id = active.preferred_id THEN 0 ELSE 1 END,
                active.requested_at ASC,
                active.id ASC
        ) AS canonical_id
    FROM active_requests active
),
superseded_requests AS (
    SELECT id, canonical_id, scope_id, scope_type
    FROM ranked_requests
    WHERE canonical_rank > 1
)
INSERT INTO knowledge.deletion_audit_logs (
    deletion_request_id,
    actor_user_id,
    action,
    resource_type,
    resource_id,
    detail
)
SELECT
    superseded.id,
    NULL,
    'deletion.superseded',
    superseded.scope_type,
    superseded.scope_id,
    jsonb_build_object(
        'canonical_request_id', superseded.canonical_id::text,
        'reason', 'message-level active request uniqueness backfill'
    )
FROM superseded_requests superseded;

WITH active_requests AS (
    SELECT
        request.id,
        request.scope_type,
        request.scope_id,
        request.requested_at,
        COALESCE(message.delete_request_id, item.delete_request_id) AS preferred_id
    FROM knowledge.deletion_requests request
    LEFT JOIN knowledge.messages message
        ON request.scope_type = 'message' AND message.id = request.scope_id
    LEFT JOIN knowledge.knowledge_items item
        ON request.scope_type = 'knowledge_item' AND item.id = request.scope_id
    WHERE request.status IN ('pending', 'approved', 'executing')
),
ranked_requests AS (
    SELECT
        active.id,
        active.scope_type,
        active.scope_id,
        active.requested_at,
        ROW_NUMBER() OVER (
            PARTITION BY active.scope_type, active.scope_id
            ORDER BY
                CASE WHEN active.id = active.preferred_id THEN 0 ELSE 1 END,
                active.requested_at ASC,
                active.id ASC
        ) AS canonical_rank
    FROM active_requests active
)
UPDATE knowledge.deletion_requests request
SET status = 'superseded',
    completed_at = COALESCE(request.completed_at, CURRENT_TIMESTAMP),
    updated_at = CURRENT_TIMESTAMP,
    last_error = NULL
FROM ranked_requests ranked
WHERE request.id = ranked.id
  AND ranked.canonical_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS deletion_requests_active_scope_uq
    ON knowledge.deletion_requests (scope_type, scope_id)
    WHERE status IN ('pending', 'approved', 'executing');

COMMIT;
