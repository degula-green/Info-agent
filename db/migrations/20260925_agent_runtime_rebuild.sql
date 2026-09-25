-- Agent runtime rebuild (Step 1).
--
-- The previous feature/agent schema kept six legacy tables. Step 1 does not
-- keep backwards compatibility: the tables are dropped and the ten canonical
-- runtime tables are created. Run the preflight first:
--
--   python services/agent/scripts/inspect_agent_schema.py
--
-- Legacy tables dropped by this migration:
--   agent.agent_tool_calls
--   agent.agent_approvals
--   agent.agent_proposals
--   agent.agent_messages
--   agent.agent_idempotency_keys
--   agent.agent_runs
--
-- Applications never drop tables at start-up; this migration is executed by
-- the explicit agent migration runner only.

BEGIN;

CREATE SCHEMA IF NOT EXISTS agent;

DO $$
DECLARE
    legacy TEXT;
BEGIN
    FOR legacy IN
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'agent' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    LOOP
        RAISE NOTICE 'agent schema table before rebuild: %', legacy;
    END LOOP;
END $$;

DROP TABLE IF EXISTS agent.agent_tool_calls CASCADE;
DROP TABLE IF EXISTS agent.agent_approvals CASCADE;
DROP TABLE IF EXISTS agent.agent_proposals CASCADE;
DROP TABLE IF EXISTS agent.agent_messages CASCADE;
DROP TABLE IF EXISTS agent.agent_idempotency_keys CASCADE;
DROP TABLE IF EXISTS agent.agent_runs CASCADE;

CREATE TABLE agent.agent_tasks (
    task_id                 TEXT PRIMARY KEY,
    source_type             VARCHAR(32)  NOT NULL CHECK (source_type IN ('chat', 'knowledge_event')),
    owner_user_id           TEXT         NOT NULL,
    status                  VARCHAR(32)  NOT NULL,
    objective               TEXT,
    current_plan_id         TEXT,
    current_plan_version    INTEGER      NOT NULL DEFAULT 0,
    idempotency_key         TEXT         UNIQUE,
    checkpoint              JSONB,
    last_error              JSONB,
    lease_owner             TEXT,
    lease_expires_at        TIMESTAMPTZ,
    next_event_sequence     BIGINT       NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ  NOT NULL,
    updated_at              TIMESTAMPTZ  NOT NULL
);

CREATE INDEX agent_tasks_status_idx ON agent.agent_tasks (status, updated_at);
CREATE INDEX agent_tasks_owner_idx ON agent.agent_tasks (owner_user_id, created_at DESC);

CREATE TABLE agent.agent_task_inputs (
    input_id     TEXT PRIMARY KEY,
    task_id      TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    version      INTEGER     NOT NULL CHECK (version > 0),
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, version)
);

CREATE TABLE agent.agent_plans (
    plan_id      TEXT PRIMARY KEY,
    task_id      TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    version      INTEGER     NOT NULL CHECK (version > 0),
    objective    TEXT        NOT NULL,
    status       VARCHAR(32) NOT NULL,
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, version)
);

CREATE UNIQUE INDEX agent_plans_single_active_idx
    ON agent.agent_plans (task_id)
    WHERE is_active;

CREATE TABLE agent.agent_plan_steps (
    step_id           TEXT PRIMARY KEY,
    plan_id           TEXT        NOT NULL REFERENCES agent.agent_plans (plan_id) ON DELETE CASCADE,
    task_id           TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    step_order        INTEGER     NOT NULL CHECK (step_order > 0),
    capability        TEXT        NOT NULL,
    arguments         JSONB       NOT NULL,
    status            VARCHAR(32) NOT NULL,
    attempt_count     INTEGER     NOT NULL DEFAULT 0,
    approved_version  INTEGER,
    last_error        JSONB,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (plan_id, step_order)
);

CREATE INDEX agent_plan_steps_status_idx ON agent.agent_plan_steps (plan_id, status, step_order);

CREATE TABLE agent.agent_observations (
    observation_id   TEXT PRIMARY KEY,
    task_id          TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    plan_id          TEXT        NOT NULL,
    step_id          TEXT        NOT NULL,
    capability       TEXT        NOT NULL,
    status           VARCHAR(32) NOT NULL,
    output           JSONB,
    error            JSONB,
    evidence         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL
);

CREATE INDEX agent_observations_task_idx ON agent.agent_observations (task_id, created_at);

CREATE TABLE agent.agent_approvals (
    approval_id   TEXT PRIMARY KEY,
    task_id       TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    plan_id       TEXT        NOT NULL,
    step_id       TEXT        NOT NULL,
    capability    TEXT        NOT NULL,
    arguments     JSONB       NOT NULL,
    version       INTEGER     NOT NULL DEFAULT 1 CHECK (version > 0),
    status        VARCHAR(32) NOT NULL,
    reason        TEXT,
    expires_at    TIMESTAMPTZ,
    decided_at    TIMESTAMPTZ,
    decided_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, step_id, version)
);

CREATE INDEX agent_approvals_pending_idx ON agent.agent_approvals (status, created_at);

CREATE TABLE agent.agent_capability_calls (
    call_id          TEXT PRIMARY KEY,
    task_id          TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    plan_id          TEXT        NOT NULL,
    step_id          TEXT        NOT NULL,
    capability       TEXT        NOT NULL,
    idempotency_key  TEXT        NOT NULL UNIQUE,
    request_id       TEXT        NOT NULL UNIQUE,
    attempt          INTEGER     NOT NULL CHECK (attempt > 0),
    status           VARCHAR(32) NOT NULL,
    arguments        JSONB       NOT NULL,
    result           JSONB,
    error            JSONB,
    created_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ
);

CREATE INDEX agent_capability_calls_step_idx ON agent.agent_capability_calls (step_id, attempt);

CREATE TABLE agent.agent_evidence (
    evidence_id     TEXT PRIMARY KEY,
    task_id         TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    plan_id         TEXT        NOT NULL,
    step_id         TEXT        NOT NULL,
    observation_id  TEXT,
    payload         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX agent_evidence_task_idx ON agent.agent_evidence (task_id, created_at);

CREATE TABLE agent.agent_outbox_events (
    event_id       TEXT PRIMARY KEY,
    task_id        TEXT,
    event_type     TEXT        NOT NULL,
    payload        JSONB       NOT NULL,
    status         VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt_count  INTEGER     NOT NULL DEFAULT 0,
    last_error     TEXT,
    available_at   TIMESTAMPTZ NOT NULL,
    published_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL
);

CREATE INDEX agent_outbox_pending_idx
    ON agent.agent_outbox_events (status, available_at)
    WHERE status = 'pending';

CREATE TABLE agent.agent_task_events (
    event_id     TEXT PRIMARY KEY,
    task_id      TEXT        NOT NULL REFERENCES agent.agent_tasks (task_id) ON DELETE CASCADE,
    sequence     BIGINT      NOT NULL CHECK (sequence > 0),
    event_type   TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (task_id, sequence)
);

CREATE INDEX agent_task_events_stream_idx ON agent.agent_task_events (task_id, sequence);

COMMIT;
