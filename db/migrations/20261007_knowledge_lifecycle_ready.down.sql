-- Collapse the ready state back to active before restoring the narrow
-- constraint, otherwise the ALTER would fail on rows this migration allowed.
UPDATE knowledge.knowledge_items SET lifecycle_status = 'active' WHERE lifecycle_status = 'ready';

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_lifecycle_chk;

ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_lifecycle_chk
    CHECK (lifecycle_status = 'active');
