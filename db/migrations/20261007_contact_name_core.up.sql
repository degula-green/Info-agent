-- Store the user's own remark for an attached contact and the extracted name
-- core ("主体名") used to resolve a person by name later. Both columns are
-- additive and nullable, so the migration is backward compatible.
BEGIN;

ALTER TABLE knowledge.contact_relations
    ADD COLUMN IF NOT EXISTS remark VARCHAR(200),
    ADD COLUMN IF NOT EXISTS name_core VARCHAR(100);

CREATE INDEX IF NOT EXISTS contact_relations_owner_name_core_idx
    ON knowledge.contact_relations (owner_user_id, name_core);

CREATE INDEX IF NOT EXISTS external_identities_display_name_idx
    ON knowledge.external_identities (display_name);

COMMIT;
