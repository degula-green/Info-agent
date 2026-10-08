BEGIN;

CREATE TABLE IF NOT EXISTS agent.agent_person_fact_snapshots (
    id UUID PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    person_key TEXT NOT NULL,
    snapshot_fingerprint TEXT NOT NULL,
    facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_person_fact_snapshots_uq
        UNIQUE (owner_user_id, person_key, snapshot_fingerprint)
);

CREATE INDEX IF NOT EXISTS agent_person_fact_snapshots_owner_idx
    ON agent.agent_person_fact_snapshots (owner_user_id, person_key, updated_at DESC);

COMMENT ON TABLE agent.agent_person_fact_snapshots IS
    'Canonical person facts keyed by the visible evidence snapshot fingerprint';

COMMIT;
