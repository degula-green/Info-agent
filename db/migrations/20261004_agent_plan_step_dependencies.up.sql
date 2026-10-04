-- Plan execution graph: NULL keeps the legacy sequential dependency; [] marks
-- an explicit independent step.

BEGIN;

ALTER TABLE agent.agent_plan_steps
    ADD COLUMN IF NOT EXISTS depends_on JSONB;

COMMENT ON COLUMN agent.agent_plan_steps.depends_on IS
    '步骤依赖 step_id 列表；NULL 表示按 order 继承前一步，[] 表示显式无依赖';

DROP INDEX IF EXISTS agent.agent_outbox_pending_idx;
CREATE INDEX IF NOT EXISTS agent_outbox_claim_idx
    ON agent.agent_outbox_events (status, available_at)
    WHERE status IN ('pending', 'publishing');

COMMIT;
