BEGIN;

DROP INDEX IF EXISTS knowledge.contact_relations_owner_name_core_idx;
DROP INDEX IF EXISTS knowledge.external_identities_display_name_idx;

ALTER TABLE knowledge.contact_relations
    DROP COLUMN IF EXISTS name_core,
    DROP COLUMN IF EXISTS remark;

COMMIT;
