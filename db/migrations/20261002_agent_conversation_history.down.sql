-- Rollback for 20261002_agent_conversation_history.up.sql.

BEGIN;

ALTER TABLE agent.agent_tasks
    DROP COLUMN IF EXISTS response_message_id,
    DROP COLUMN IF EXISTS request_message_id,
    DROP COLUMN IF EXISTS conversation_id;

DROP TABLE IF EXISTS agent.messages CASCADE;
DROP TABLE IF EXISTS agent.conversations CASCADE;

COMMIT;
