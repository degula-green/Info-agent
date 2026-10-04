BEGIN;

DROP INDEX IF EXISTS agent.agent_outbox_claim_idx;
CREATE INDEX IF NOT EXISTS agent_outbox_pending_idx
    ON agent.agent_outbox_events (status, available_at)
    WHERE status = 'pending';

ALTER TABLE agent.agent_plan_steps
    DROP COLUMN IF EXISTS depends_on;

COMMIT;
