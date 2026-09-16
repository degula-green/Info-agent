-- A contact selection is a user-owned relationship to an external identity.
-- It deliberately does not copy provider messages or files. The identity is
-- still shared with the existing ingestion model, while this table records
-- which identities a user explicitly chose to keep in their contact list.
CREATE TABLE IF NOT EXISTS knowledge.contact_relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL,
    connector_account_id UUID NOT NULL REFERENCES knowledge.connector_accounts(id) ON DELETE CASCADE,
    external_identity_id UUID NOT NULL REFERENCES knowledge.external_identities(id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_user_id, external_identity_id)
);
CREATE INDEX IF NOT EXISTS contact_relations_owner_status_idx
    ON knowledge.contact_relations (owner_user_id, status, created_at);
CREATE INDEX IF NOT EXISTS contact_relations_identity_idx
    ON knowledge.contact_relations (external_identity_id, status);
