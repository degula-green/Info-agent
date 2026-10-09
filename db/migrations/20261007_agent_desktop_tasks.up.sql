CREATE TABLE IF NOT EXISTS agent.desktop_tasks (
    desktop_task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_task_id TEXT REFERENCES agent.agent_tasks(task_id) ON DELETE SET NULL,
    agent_step_id TEXT REFERENCES agent.agent_plan_steps(step_id) ON DELETE SET NULL,
    device_id UUID NOT NULL,
    operation VARCHAR(32) NOT NULL,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    result JSONB,
    result_hash CHAR(64),
    error_code VARCHAR(64),
    error_message TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner VARCHAR(128),
    lease_expires_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    side_effect_state VARCHAR(32) NOT NULL DEFAULT 'none',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (operation IN (
        'create_session',
        'close_session',
        'open',
        'read_grid',
        'write_grid',
        'clear_range',
        'read_form',
        'fill_form',
        'submit_form',
        'screenshot',
        'send_input'
    )),
    CHECK (status IN (
        'pending',
        'delivered',
        'running',
        'waiting_login',
        'completed',
        'failed',
        'expired',
        'needs_review'
    )),
    CHECK (attempt >= 0),
    CHECK (side_effect_state IN ('none', 'pending', 'committed', 'unknown')),
    CHECK (jsonb_typeof(request_payload) = 'object'),
    CHECK (result IS NULL OR jsonb_typeof(result) = 'object')
);

CREATE INDEX IF NOT EXISTS desktop_tasks_device_pending_idx
    ON agent.desktop_tasks (device_id, status, created_at);

CREATE INDEX IF NOT EXISTS desktop_tasks_agent_task_idx
    ON agent.desktop_tasks (agent_task_id, created_at);

CREATE INDEX IF NOT EXISTS desktop_tasks_expiry_idx
    ON agent.desktop_tasks (expires_at)
    WHERE status IN ('pending', 'delivered', 'running', 'waiting_login');

CREATE INDEX IF NOT EXISTS desktop_tasks_lease_idx
    ON agent.desktop_tasks (lease_expires_at)
    WHERE status IN ('delivered', 'running');

COMMENT ON TABLE agent.desktop_tasks IS
    'Agent 下发到桌面端执行的浏览器原子操作任务';
