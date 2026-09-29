-- Step 2.5 / Step 3: semantic understanding and dynamic planning state.

ALTER TABLE agent.agent_tasks
    ADD COLUMN IF NOT EXISTS understanding JSONB,
    ADD COLUMN IF NOT EXISTS result JSONB,
    ADD COLUMN IF NOT EXISTS replan_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS step_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS model_call_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE agent.agent_plans
    ADD COLUMN IF NOT EXISTS parent_plan_id TEXT,
    ADD COLUMN IF NOT EXISTS triggered_by_observation_id TEXT,
    ADD COLUMN IF NOT EXISTS replan_reason TEXT,
    ADD COLUMN IF NOT EXISTS unsupported_intents JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS requires_user_confirmation BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE agent.agent_plan_steps
    ADD COLUMN IF NOT EXISTS replaced_by_step_id TEXT;

CREATE INDEX IF NOT EXISTS agent_plans_parent_idx
    ON agent.agent_plans (parent_plan_id)
    WHERE parent_plan_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS agent_plan_steps_replaced_idx
    ON agent.agent_plan_steps (replaced_by_step_id)
    WHERE replaced_by_step_id IS NOT NULL;
