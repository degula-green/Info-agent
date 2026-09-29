-- Step 2: Knowledge side of the Agent calendar capability.
--
-- calendar_authorizations records *who may write a calendar* and points at the
-- Vault credential; tokens never live in this table.
-- calendar_event_requests is the request_id ledger that makes the create call
-- idempotent for the Agent.

CREATE TABLE IF NOT EXISTS knowledge.calendar_authorizations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id       UUID        NOT NULL,
    provider            VARCHAR(32) NOT NULL,
    credential_ref      TEXT        NOT NULL,
    external_account_id VARCHAR(255),
    status              VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT calendar_authorizations_owner_uq UNIQUE (owner_user_id, provider),
    CONSTRAINT calendar_authorizations_status_chk CHECK (status IN ('active', 'revoked'))
);

CREATE TABLE IF NOT EXISTS knowledge.calendar_event_requests (
    request_id    TEXT PRIMARY KEY,
    owner_user_id UUID        NOT NULL,
    provider      VARCHAR(32) NOT NULL,
    status        VARCHAR(16) NOT NULL,
    event_id      VARCHAR(255) NOT NULL,
    event_url     TEXT,
    title         TEXT,
    start_time    TIMESTAMPTZ,
    end_time      TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT calendar_event_requests_status_chk CHECK (status IN ('created'))
);

CREATE INDEX IF NOT EXISTS calendar_event_requests_owner_idx
    ON knowledge.calendar_event_requests (owner_user_id, created_at DESC);

COMMENT ON TABLE knowledge.calendar_authorizations IS '日历写入授权；Token 存 Token Vault，本表只存引用';
COMMENT ON TABLE knowledge.calendar_event_requests IS 'Agent 日历写入的 request_id 幂等账本';
