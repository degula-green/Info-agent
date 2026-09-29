BEGIN;

UPDATE knowledge.knowledge_items
SET security_status = 'not_required',
    security_ready = TRUE
WHERE source_type = 'private_conversation'
  AND security_status = 'classified';

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_security_not_required_chk;

ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_security_not_required_chk CHECK (
        source_type = 'platform_conversation'
        OR (security_status = 'not_required' AND security_ready)
    );

COMMIT;
