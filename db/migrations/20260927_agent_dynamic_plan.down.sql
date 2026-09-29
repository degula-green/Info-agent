ALTER TABLE agent.agent_plan_steps
    DROP COLUMN IF EXISTS replaced_by_step_id;

ALTER TABLE agent.agent_plans
    DROP COLUMN IF EXISTS requires_user_confirmation,
    DROP COLUMN IF EXISTS warnings,
    DROP COLUMN IF EXISTS unsupported_intents,
    DROP COLUMN IF EXISTS replan_reason,
    DROP COLUMN IF EXISTS triggered_by_observation_id,
    DROP COLUMN IF EXISTS parent_plan_id;

ALTER TABLE agent.agent_tasks
    DROP COLUMN IF EXISTS model_call_count,
    DROP COLUMN IF EXISTS step_count,
    DROP COLUMN IF EXISTS replan_count,
    DROP COLUMN IF EXISTS result,
    DROP COLUMN IF EXISTS understanding;
