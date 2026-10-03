-- Rollback for Phase 1 conversation summary jobs.

BEGIN;

DROP TABLE IF EXISTS agent.conversation_summary_jobs CASCADE;

ALTER TABLE agent.conversations
    DROP CONSTRAINT IF EXISTS conversations_summary_until_message_fk,
    DROP COLUMN IF EXISTS summary_token_count,
    DROP COLUMN IF EXISTS summary_method,
    DROP COLUMN IF EXISTS summary_updated_at,
    DROP COLUMN IF EXISTS summary_version,
    DROP COLUMN IF EXISTS summary_until_message_id;

COMMIT;
