BEGIN;

ALTER TABLE knowledge.messages
    DROP CONSTRAINT IF EXISTS messages_content_hash_chk;
ALTER TABLE knowledge.messages
    ADD CONSTRAINT messages_content_hash_chk
    CHECK (content_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_content_hash_chk;
ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_content_hash_chk
    CHECK (content_hash ~ '^[0-9a-f]{64}$');

COMMIT;
