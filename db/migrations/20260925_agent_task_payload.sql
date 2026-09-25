-- Persist the Task payload that the runtime re-reads on every drive.
--
-- Step 1 created agent.agent_tasks without the request payload, so rebuilding a
-- Task from PostgreSQL lost ``input`` / ``source_ref`` / ``constraints`` and the
-- planner saw an empty task. The input history still lives in agent_task_inputs;
-- these columns carry the *current* authoritative values.

ALTER TABLE agent.agent_tasks
    ADD COLUMN IF NOT EXISTS input JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS source_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS constraints JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN agent.agent_tasks.input IS '当前任务输入（最新一次合并结果）';
COMMENT ON COLUMN agent.agent_tasks.source_ref IS '来源引用，例如 knowledge_item_id/content_version/sent_at';
COMMENT ON COLUMN agent.agent_tasks.constraints IS '任务约束';
