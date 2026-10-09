-- Supersede deletion requests that were created but never executed.
--
-- Run db/migrations/20261006_message_deletion_dedup.up.sql first: this script
-- uses the `superseded` status added by that migration.
--
-- Background: the deletion flow recorded a request while its target message
-- stayed `active`, and every click produced a fresh idempotency key. That left
-- rows in `executing`/`pending` whose targets were never actually hidden or
-- cleaned. Requests whose target already reached `deleting` are legitimate
-- deletions and are deliberately preserved here.
--
-- Safety properties:
--   * Only requests whose target message is still `active` are superseded.
--   * A message/item that carries `delete_request_id` is never touched, so no
--     preserved deletion loses its back-reference.
--   * Every affected request is first copied into a backup table, so the change
--     can be audited and undone.
--
-- Usage:
--   psql "$CORE_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/cleanup-zombie-deletion-requests.sql

BEGIN;

CREATE TABLE IF NOT EXISTS knowledge.deletion_requests_zombie_backup (
    backed_up_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    id                   UUID NOT NULL,
    organization_id      UUID,
    requester_user_id    UUID NOT NULL,
    reviewer_user_id     UUID,
    scope_type           VARCHAR(32) NOT NULL,
    scope_id             UUID NOT NULL,
    status               VARCHAR(32) NOT NULL,
    reason               TEXT NOT NULL,
    idempotency_key      VARCHAR(200) NOT NULL,
    requested_at         TIMESTAMPTZ NOT NULL,
    reviewed_at          TIMESTAMPTZ,
    execution_started_at TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    purge_after          TIMESTAMPTZ,
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL
);

-- Snapshot the rows that are about to be removed.
INSERT INTO knowledge.deletion_requests_zombie_backup (
    id, organization_id, requester_user_id, reviewer_user_id, scope_type,
    scope_id, status, reason, idempotency_key, requested_at, reviewed_at,
    execution_started_at, completed_at, purge_after, last_error,
    created_at, updated_at
)
SELECT r.id, r.organization_id, r.requester_user_id, r.reviewer_user_id,
       r.scope_type, r.scope_id, r.status, r.reason, r.idempotency_key,
       r.requested_at, r.reviewed_at, r.execution_started_at, r.completed_at,
       r.purge_after, r.last_error, r.created_at, r.updated_at
FROM knowledge.deletion_requests r
WHERE EXISTS (
        SELECT 1
        FROM knowledge.deletion_targets t
        JOIN knowledge.messages m ON m.id = t.resource_id
        WHERE t.deletion_request_id = r.id
          AND m.lifecycle_status = 'active'
      )
  AND NOT EXISTS (
        SELECT 1
        FROM knowledge.messages m
        WHERE m.delete_request_id = r.id
      );

-- Keep the audit trail. Mark only the snapshotted requests as superseded.
UPDATE knowledge.deletion_requests r
SET status = 'superseded',
    completed_at = COALESCE(r.completed_at, CURRENT_TIMESTAMP),
    updated_at = CURRENT_TIMESTAMP,
    last_error = NULL
WHERE EXISTS (
        SELECT 1
        FROM knowledge.deletion_requests_zombie_backup b
        WHERE b.id = r.id
      )
  AND EXISTS (
        SELECT 1
        FROM knowledge.deletion_targets t
        JOIN knowledge.messages m ON m.id = t.resource_id
        WHERE t.deletion_request_id = r.id
          AND m.lifecycle_status = 'active'
      );

COMMIT;

-- Verification (read-only). Expect: every backed-up row is no longer active,
-- except rows that became active again after this script ran, and every
-- remaining request points at a message that is no longer `active`.
SELECT 'backed_up' AS metric, count(*) AS value
FROM knowledge.deletion_requests_zombie_backup
UNION ALL
SELECT 'remaining_active_target', count(*)
FROM knowledge.deletion_requests r
JOIN knowledge.deletion_targets t ON t.deletion_request_id = r.id
JOIN knowledge.messages m ON m.id = t.resource_id
WHERE m.lifecycle_status = 'active';
