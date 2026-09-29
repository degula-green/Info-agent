-- Rollback for 20260927_agent_todo_ledger.sql.

BEGIN;

DROP TABLE IF EXISTS agent.agent_todos CASCADE;

COMMIT;

