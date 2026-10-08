DROP TABLE IF EXISTS knowledge.wechat_snapshots;
DROP TABLE IF EXISTS knowledge.wechat_commands;

ALTER TABLE knowledge.wechat_collection_configs
    DROP CONSTRAINT IF EXISTS wechat_collection_configs_desired_status_chk;

ALTER TABLE knowledge.wechat_collection_configs
    DROP COLUMN IF EXISTS desired_status;
