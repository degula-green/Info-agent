UPDATE knowledge.conversation_collectors
SET status = 'active', updated_at = now()
WHERE status = 'paused';

ALTER TABLE knowledge.conversation_collectors
    DROP CONSTRAINT IF EXISTS conversation_collectors_status_chk;

ALTER TABLE knowledge.conversation_collectors
    ADD CONSTRAINT conversation_collectors_status_chk
    CHECK (status IN ('active', 'unavailable', 'removed'));

COMMENT ON COLUMN knowledge.conversation_collectors.status IS
    '采集者状态：active、unavailable、removed';
