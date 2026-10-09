BEGIN;

DROP TABLE IF EXISTS knowledge.deletion_audit_logs;
DROP TABLE IF EXISTS knowledge.deletion_targets;
DROP TABLE IF EXISTS knowledge.deletion_requests;

DROP INDEX IF EXISTS knowledge.knowledge_items_delete_request_idx;
DROP INDEX IF EXISTS knowledge.knowledge_items_lifecycle_idx;
DROP INDEX IF EXISTS knowledge.messages_delete_request_idx;
DROP INDEX IF EXISTS knowledge.messages_lifecycle_idx;

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_vector_delete_status_chk;
ALTER TABLE knowledge.messages
    DROP CONSTRAINT IF EXISTS messages_vector_delete_status_chk;

ALTER TABLE knowledge.knowledge_items
    DROP COLUMN IF EXISTS vector_delete_status,
    DROP COLUMN IF EXISTS content_purged_at,
    DROP COLUMN IF EXISTS purge_after,
    DROP COLUMN IF EXISTS delete_request_id,
    DROP COLUMN IF EXISTS delete_reason,
    DROP COLUMN IF EXISTS deleted_by_user_id,
    DROP COLUMN IF EXISTS deleted_at;

ALTER TABLE knowledge.messages
    DROP COLUMN IF EXISTS vector_delete_status,
    DROP COLUMN IF EXISTS content_purged_at,
    DROP COLUMN IF EXISTS purge_after,
    DROP COLUMN IF EXISTS delete_request_id,
    DROP COLUMN IF EXISTS delete_reason,
    DROP COLUMN IF EXISTS deleted_by_user_id,
    DROP COLUMN IF EXISTS deleted_at;

ALTER TABLE knowledge.messages
    DROP CONSTRAINT IF EXISTS messages_lifecycle_chk;
ALTER TABLE knowledge.messages
    ADD CONSTRAINT messages_lifecycle_chk CHECK (lifecycle_status = 'active');

ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_lifecycle_chk;
ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_lifecycle_chk CHECK (lifecycle_status = 'active');

COMMIT;
