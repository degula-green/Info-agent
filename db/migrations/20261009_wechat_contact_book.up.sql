-- Owner-scoped snapshot of a WeChat owner's local contact book.
--
-- The collector reads its local WeChat ``contact.db``, where the ``remark``
-- column is a personal label the machine owner wrote. That label must never be
-- a shared, cross-user name source, so each owner's contact book is stored here
-- keyed by ``owner_user_id`` and resolved only for that owner.
BEGIN;

CREATE TABLE IF NOT EXISTS knowledge.wechat_contact_book (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL,
    connector_account_id UUID NOT NULL REFERENCES knowledge.connector_accounts (id) ON DELETE CASCADE,
    external_user_id VARCHAR(255) NOT NULL,
    nick_name VARCHAR(255) NOT NULL DEFAULT '',
    remark VARCHAR(255) NOT NULL DEFAULT '',
    name_core VARCHAR(100) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT wechat_contact_book_owner_connector_external_uq
        UNIQUE (owner_user_id, connector_account_id, external_user_id)
);

CREATE INDEX IF NOT EXISTS wechat_contact_book_owner_status_name_core_idx
    ON knowledge.wechat_contact_book (owner_user_id, status, name_core);
CREATE INDEX IF NOT EXISTS wechat_contact_book_owner_status_external_idx
    ON knowledge.wechat_contact_book (owner_user_id, status, external_user_id);

COMMENT ON TABLE knowledge.wechat_contact_book IS '微信所有者本机通讯录的按用户隔离快照；备注只归所有者使用';
COMMENT ON COLUMN knowledge.wechat_contact_book.owner_user_id IS '通讯录所属用户；解析时只能命中这一列等于请求者的行';
COMMENT ON COLUMN knowledge.wechat_contact_book.remark IS '本机微信中机主给联系人写的备注，个人标签';
COMMENT ON COLUMN knowledge.wechat_contact_book.name_core IS '由备注或昵称提取的主体名，用于按备注解析';

COMMIT;
