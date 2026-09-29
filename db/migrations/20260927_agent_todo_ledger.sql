-- Step 6: the Agent's own to-do ledger.
--
-- "日程" and "待办" were merged into one product object: anything the owner must
-- still act on. The row is written by the todo.create Capability, lives in the
-- Agent schema, and has no external calendar or Knowledge counterpart.
--
-- due_at is nullable on purpose. "完成登录模块代码" has no time at all; an
-- uncertain time must not block the write, so the raw phrase is kept in
-- due_expression and the desktop owns the final value.
--
-- The row is NOT deleted when the preview is confirmed: the preview belongs to
-- the Task, the to-do belongs to the desktop. Deletion is the owner's action.

BEGIN;

CREATE TABLE IF NOT EXISTS agent.agent_todos (
    todo_id         TEXT PRIMARY KEY,
    owner_user_id   TEXT         NOT NULL,
    title           TEXT         NOT NULL,
    due_at          TIMESTAMPTZ,
    due_expression  TEXT,
    timezone        VARCHAR(64),
    notes           TEXT,
    status          VARCHAR(16)  NOT NULL DEFAULT 'open',
    source          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    plan_id         TEXT,
    step_id         TEXT,
    idempotency_key TEXT         UNIQUE,
    created_at      TIMESTAMPTZ  NOT NULL,
    updated_at      TIMESTAMPTZ  NOT NULL,
    completed_at    TIMESTAMPTZ,
    CONSTRAINT agent_todos_status_chk CHECK (status IN ('open', 'done', 'cancelled'))
);

-- The desktop reads "my unfinished items first"; an overdue row is still
-- unfinished, so status is not part of this index.
CREATE INDEX IF NOT EXISTS agent_todos_owner_due_idx
    ON agent.agent_todos (owner_user_id, due_at ASC NULLS LAST, created_at ASC);

CREATE INDEX IF NOT EXISTS agent_todos_status_idx
    ON agent.agent_todos (status, updated_at DESC);

COMMENT ON TABLE agent.agent_todos IS 'Agent 自有的待办账本；日程与待办已合并为同一对象';
COMMENT ON COLUMN agent.agent_todos.due_at IS '可选截止时间；为空表示只存在待处理这件事';
COMMENT ON COLUMN agent.agent_todos.due_expression IS '用户原话中的时间短语，due_at 无法解析时仍保留';
COMMENT ON COLUMN agent.agent_todos.idempotency_key IS '一次消息一条待办；重试复用同一行';
COMMENT ON COLUMN agent.agent_todos.status IS 'open/done/cancelled；逾期不自动关闭';

COMMIT;

