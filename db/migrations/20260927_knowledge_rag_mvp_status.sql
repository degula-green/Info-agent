BEGIN;

UPDATE knowledge.knowledge_items
SET rag_status = 'ready'
WHERE rag_status = 'succeeded';

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_rag_status_chk;

ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_rag_status_chk
    CHECK (
        rag_status IN (
            'pending', 'processing', 'ready', 'metadata_only', 'failed', 'cancelled'
        )
    );

COMMIT;
