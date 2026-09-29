BEGIN;

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_security_not_required_chk;

ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_security_not_required_chk CHECK (
        source_type = 'platform_conversation'
        OR (
            security_status IN ('not_required', 'classified')
            AND security_ready
        )
    );

COMMIT;
