ALTER TABLE agent.agent_tasks
    DROP COLUMN IF EXISTS constraints,
    DROP COLUMN IF EXISTS source_ref,
    DROP COLUMN IF EXISTS input;
