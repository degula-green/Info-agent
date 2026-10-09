-- The superseded audit trail is intentionally not rewritten. Reverting the
-- migration only removes the new concurrency guard.
DROP INDEX IF EXISTS knowledge.deletion_requests_active_scope_uq;
