-- Phase 1: user-visible Agent conversations and messages.
--
-- A Conversation is the history window the Agent page restores. A Message is
-- only what the user sees; Plan Steps and Observations remain execution detail.
-- Existing agent_tasks.owner_user_id and task_id are TEXT, so every column that
-- references them keeps TEXT instead of introducing a UUID/TEXT mismatch.

BEGIN;

CREATE TABLE IF NOT EXISTS agent.conversations (
    conversation_id uuid PRIMARY KEY,
    owner_user_id   text         NOT NULL,
    organization_id text,
    title           varchar(200) NOT NULL DEFAULT '新的对话',
    status          varchar(32)  NOT NULL DEFAULT 'active',
    source          varchar(32)  NOT NULL DEFAULT 'agent',
    summary         text,
    summary_cursor  integer      NOT NULL DEFAULT 0,
    last_message_at timestamptz,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT conversations_status_chk
        CHECK (status IN ('active', 'archived'))
);

CREATE TABLE IF NOT EXISTS agent.messages (
    message_id        uuid PRIMARY KEY,
    conversation_id   uuid         NOT NULL
        REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
    role              varchar(32)  NOT NULL,
    content           text         NOT NULL DEFAULT '',
    status            varchar(32)  NOT NULL DEFAULT 'pending',
    task_id           text,
    citations         jsonb        NOT NULL DEFAULT '[]'::jsonb,
    client_message_id varchar(128),
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_chk
        CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT messages_status_chk
        CHECK (status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled'))
);

ALTER TABLE agent.agent_tasks
    ADD COLUMN IF NOT EXISTS conversation_id uuid
        REFERENCES agent.conversations(conversation_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS request_message_id uuid,
    ADD COLUMN IF NOT EXISTS response_message_id uuid;

CREATE INDEX IF NOT EXISTS conversations_owner_recent_idx
    ON agent.conversations (owner_user_id, last_message_at DESC NULLS LAST, created_at DESC);

CREATE INDEX IF NOT EXISTS messages_conversation_created_idx
    ON agent.messages (conversation_id, created_at ASC);

CREATE INDEX IF NOT EXISTS messages_task_idx
    ON agent.messages (task_id)
    WHERE task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS agent_tasks_conversation_idx
    ON agent.agent_tasks (conversation_id)
    WHERE conversation_id IS NOT NULL;

COMMENT ON TABLE agent.conversations IS 'Agent 用户可见会话窗口';
COMMENT ON COLUMN agent.conversations.summary_cursor IS '会话摘要已消费到的消息序号，Phase 4 使用';
COMMENT ON TABLE agent.messages IS 'Agent 用户可见消息；执行步骤不写入此表';
COMMENT ON COLUMN agent.messages.task_id IS '触发或产出该消息的 agent_tasks.task_id，保持 TEXT 类型';

COMMIT;
