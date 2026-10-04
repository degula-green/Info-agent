-- Explicit, historical device-to-connector ownership.
--
-- agent_devices.connector_id remains temporarily as a compatibility pointer,
-- but new reads should resolve ownership through this table.

BEGIN;

CREATE TABLE IF NOT EXISTS knowledge.wechat_pairings (
    id UUID PRIMARY KEY,
    owner_user_id UUID NOT NULL,
    code_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'consumed', 'expired', 'failed')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    wxid VARCHAR(255),
    database_ref CHAR(64),
    default_organization_id UUID,
    device_id UUID,
    connector_id UUID,
    failure_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge.agent_devices (
    id UUID PRIMARY KEY,
    connector_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id),
    owner_user_id UUID NOT NULL,
    key_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    agent_version VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge.agent_device_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES knowledge.agent_devices (id) ON DELETE CASCADE,
    connector_id UUID NOT NULL REFERENCES knowledge.connector_accounts (id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('active', 'revoked')),
    assigned_by_user_id UUID,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_by_user_id UUID,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT agent_device_assignments_state_chk CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

-- If legacy data contains more than one active device for a connector, keep
-- the most recently created device and revoke the rest before the unique
-- indexes are created.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY connector_id
               ORDER BY created_at DESC, id DESC
           ) AS position
    FROM knowledge.agent_devices
    WHERE revoked_at IS NULL
      AND connector_id IS NOT NULL
)
UPDATE knowledge.agent_devices AS device
SET revoked_at = CURRENT_TIMESTAMP
FROM ranked
WHERE device.id = ranked.id
  AND ranked.position > 1;

INSERT INTO knowledge.agent_device_assignments (
    device_id,
    connector_id,
    status,
    assigned_by_user_id,
    assigned_at
)
SELECT device.id,
       device.connector_id,
       'active',
       device.owner_user_id,
       device.created_at
FROM knowledge.agent_devices AS device
WHERE device.revoked_at IS NULL
  AND device.connector_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM knowledge.agent_device_assignments AS assignment
      WHERE assignment.device_id = device.id
        AND assignment.connector_id = device.connector_id
        AND assignment.status = 'active'
  );

CREATE UNIQUE INDEX IF NOT EXISTS agent_device_assignments_active_connector_uq
    ON knowledge.agent_device_assignments (connector_id)
    WHERE status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS agent_device_assignments_active_device_uq
    ON knowledge.agent_device_assignments (device_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS agent_device_assignments_device_history_idx
    ON knowledge.agent_device_assignments (device_id, assigned_at DESC);

CREATE INDEX IF NOT EXISTS agent_device_assignments_connector_history_idx
    ON knowledge.agent_device_assignments (connector_id, assigned_at DESC);

COMMENT ON TABLE knowledge.agent_device_assignments IS
    '设备与连接器的归属历史；同一连接器和设备各自最多一条 active 记录';

COMMIT;
