-- Step 3: bind an approval to the exact Step arguments the user approved.
--
-- Replanning may rewrite a Step while reusing its step_id; without this
-- fingerprint an old approval could authorise new write arguments.

ALTER TABLE agent.agent_approvals
    ADD COLUMN IF NOT EXISTS arguments_hash TEXT;

COMMENT ON COLUMN agent.agent_approvals.arguments_hash IS
    '批准时步骤参数的 SHA-256 指纹；与当前参数不一致时必须重新审批';
