-- Phase 1 conversation memory: rolling summary state and durable summary jobs.

BEGIN;

ALTER TABLE agent.conversations
    ADD COLUMN IF NOT EXISTS summary_until_message_id uuid,
    ADD COLUMN IF NOT EXISTS summary_version bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS summary_updated_at timestamptz,
    ADD COLUMN IF NOT EXISTS summary_method varchar(32) NOT NULL DEFAULT 'incremental',
    ADD COLUMN IF NOT EXISTS summary_token_count integer NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'conversations_summary_until_message_fk'
          AND conrelid = 'agent.conversations'::regclass
    ) THEN
        ALTER TABLE agent.conversations
            ADD CONSTRAINT conversations_summary_until_message_fk
            FOREIGN KEY (summary_until_message_id)
            REFERENCES agent.messages(message_id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS agent.conversation_summary_jobs (
    job_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL
        REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
    expected_summary_version bigint NOT NULL,
    boundary_from_message_id uuid,
    boundary_to_message_id uuid NOT NULL
        REFERENCES agent.messages(message_id) ON DELETE CASCADE,
    status varchar(32) NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner varchar(128),
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT conversation_summary_jobs_status_chk
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    UNIQUE (conversation_id, expected_summary_version, boundary_to_message_id)
);

CREATE INDEX IF NOT EXISTS conversation_summary_jobs_claim_idx
    ON agent.conversation_summary_jobs (status, available_at, created_at)
    WHERE status IN ('pending', 'failed');

COMMIT;
