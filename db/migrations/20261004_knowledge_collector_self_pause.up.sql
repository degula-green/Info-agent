ALTER TABLE knowledge.conversation_collectors
    DROP CONSTRAINT IF EXISTS conversation_collectors_status_chk;

ALTER TABLE knowledge.conversation_collectors
    ADD CONSTRAINT conversation_collectors_status_chk
    CHECK (status IN ('active', 'paused', 'unavailable', 'removed'));

COMMENT ON COLUMN knowledge.conversation_collectors.status IS
    '采集者状态：active、paused、unavailable、removed';
