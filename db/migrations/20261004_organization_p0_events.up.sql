BEGIN;

CREATE TABLE IF NOT EXISTS iam.outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(100) NOT NULL,
    aggregate_type VARCHAR(64) NOT NULL DEFAULT 'organization_membership',
    aggregate_id UUID NOT NULL,
    organization_id UUID NOT NULL,
    user_id UUID NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT iam_outbox_events_status_chk
        CHECK (status IN ('pending', 'publishing', 'published', 'failed')),
    CONSTRAINT iam_outbox_events_attempts_chk CHECK (publish_attempts >= 0)
);

CREATE INDEX IF NOT EXISTS iam_outbox_events_pending_idx
    ON iam.outbox_events (available_at, created_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS iam_outbox_events_org_idx
    ON iam.outbox_events (organization_id, created_at DESC);

COMMIT;
