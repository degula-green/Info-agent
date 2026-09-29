-- Reverses 20260925_agent_runtime_rebuild.sql.
--
-- The migration is destructive by design: the legacy feature/agent tables are
-- not restored. Dropping the runtime tables returns the schema to empty.

BEGIN;

DROP TABLE IF EXISTS agent.agent_task_events CASCADE;
DROP TABLE IF EXISTS agent.agent_outbox_events CASCADE;
DROP TABLE IF EXISTS agent.agent_evidence CASCADE;
DROP TABLE IF EXISTS agent.agent_capability_calls CASCADE;
DROP TABLE IF EXISTS agent.agent_approvals CASCADE;
DROP TABLE IF EXISTS agent.agent_observations CASCADE;
DROP TABLE IF EXISTS agent.agent_plan_steps CASCADE;
DROP TABLE IF EXISTS agent.agent_plans CASCADE;
DROP TABLE IF EXISTS agent.agent_task_inputs CASCADE;
DROP TABLE IF EXISTS agent.agent_tasks CASCADE;

COMMIT;
