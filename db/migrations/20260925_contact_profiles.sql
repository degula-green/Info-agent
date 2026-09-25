BEGIN;

CREATE TABLE IF NOT EXISTS knowledge.contact_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES knowledge.messages (id) ON DELETE CASCADE,
    conversation_ingestion_id UUID NOT NULL REFERENCES knowledge.conversation_ingestions (id) ON DELETE CASCADE,
    sender_identity_id UUID NOT NULL REFERENCES knowledge.external_identities (id) ON DELETE CASCADE,
    fact_type VARCHAR(16) NOT NULL CHECK (fact_type IN ('phone', 'email', 'id_card', 'bank_card', 'secret', 'db_config')),
    label VARCHAR(64) NOT NULL,
    raw_value TEXT NOT NULL,
    value_hash VARCHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT contact_facts_message_type_hash_uq UNIQUE (message_id, fact_type, value_hash)
);

CREATE INDEX IF NOT EXISTS contact_facts_sender_type_idx
    ON knowledge.contact_facts (sender_identity_id, fact_type);
CREATE INDEX IF NOT EXISTS contact_facts_message_idx
    ON knowledge.contact_facts (message_id);
CREATE INDEX IF NOT EXISTS contact_facts_value_hash_idx
    ON knowledge.contact_facts (value_hash);

CREATE TABLE IF NOT EXISTS knowledge.contact_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL,
    contact_key VARCHAR(128) NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    source_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'failed')),
    last_error TEXT,
    generated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT contact_profiles_owner_contact_uq UNIQUE (owner_user_id, contact_key)
);

ALTER TABLE knowledge.messages
    ADD COLUMN IF NOT EXISTS contact_facts_status VARCHAR(16) NOT NULL DEFAULT 'pending'
    CHECK (contact_facts_status IN ('pending', 'succeeded', 'failed'));

CREATE INDEX IF NOT EXISTS messages_contact_facts_pending_idx
    ON knowledge.messages (created_at)
    WHERE contact_facts_status = 'pending';

CREATE INDEX IF NOT EXISTS private_access_requests_requester_status_created_idx
    ON knowledge.private_access_requests (requester_user_id, status, created_at);

COMMIT;
