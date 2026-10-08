ALTER TABLE knowledge.wechat_collection_configs
    ADD COLUMN IF NOT EXISTS desired_status VARCHAR(32);

UPDATE knowledge.wechat_collection_configs
SET desired_status = CASE WHEN enabled THEN 'running' ELSE 'stopped' END
WHERE desired_status IS NULL OR btrim(desired_status) = '';

ALTER TABLE knowledge.wechat_collection_configs
    ALTER COLUMN desired_status SET DEFAULT 'running';

ALTER TABLE knowledge.wechat_collection_configs
    ALTER COLUMN desired_status SET NOT NULL;

ALTER TABLE knowledge.wechat_collection_configs
    DROP CONSTRAINT IF EXISTS wechat_collection_configs_desired_status_chk;

ALTER TABLE knowledge.wechat_collection_configs
    ADD CONSTRAINT wechat_collection_configs_desired_status_chk
    CHECK (desired_status IN ('stopped', 'running', 'paused'));

CREATE TABLE IF NOT EXISTS knowledge.wechat_commands (
    command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id) ON DELETE CASCADE,
    device_id UUID REFERENCES knowledge.agent_devices(id) ON DELETE SET NULL,
    command_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    deadline TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_code VARCHAR(64),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (command_type IN (
        'wechat.collector.start',
        'wechat.collector.stop',
        'wechat.collector.apply_config'
    )),
    CHECK (status IN (
        'pending',
        'delivered',
        'running',
        'acknowledged',
        'failed',
        'expired'
    )),
    CHECK (attempt >= 0),
    CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX IF NOT EXISTS wechat_commands_connector_pending_idx
    ON knowledge.wechat_commands (connector_id, status, created_at);

CREATE INDEX IF NOT EXISTS wechat_commands_device_pending_idx
    ON knowledge.wechat_commands (device_id, status, created_at);

CREATE INDEX IF NOT EXISTS wechat_commands_deadline_idx
    ON knowledge.wechat_commands (deadline)
    WHERE status IN ('pending', 'delivered', 'running');

CREATE TABLE IF NOT EXISTS knowledge.wechat_snapshots (
    connector_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id) ON DELETE CASCADE,
    snapshot_type VARCHAR(32) NOT NULL,
    device_id UUID REFERENCES knowledge.agent_devices(id) ON DELETE SET NULL,
    version BIGINT NOT NULL,
    items JSONB NOT NULL DEFAULT '[]'::jsonb,
    captured_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (connector_id, snapshot_type),
    CHECK (snapshot_type IN ('conversations', 'contacts')),
    CHECK (version >= 0),
    CHECK (jsonb_typeof(items) = 'array')
);

CREATE INDEX IF NOT EXISTS wechat_snapshots_expiry_idx
    ON knowledge.wechat_snapshots (expires_at);

COMMENT ON COLUMN knowledge.wechat_collection_configs.desired_status IS
    '服务端期望的桌面采集器状态：stopped、running、paused';

COMMENT ON TABLE knowledge.wechat_commands IS
    'Knowledge 下发给已配对桌面设备的幂等命令';

COMMENT ON TABLE knowledge.wechat_snapshots IS
    '桌面 WeChat Collector 主动上报的会话和联系人快照';
