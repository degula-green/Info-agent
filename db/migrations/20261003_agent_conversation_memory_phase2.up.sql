-- Phase 2: conversation-scoped structured memory.

BEGIN;

CREATE TABLE IF NOT EXISTS agent.memory_records (
    memory_id uuid PRIMARY KEY,
    owner_user_id text NOT NULL,
    organization_id text,
    memory_type varchar(32) NOT NULL,
    scope varchar(32) NOT NULL DEFAULT 'conversation',
    title varchar(200) NOT NULL,
    content text NOT NULL,
    content_hash char(64) NOT NULL,
    memory_key varchar(128) NOT NULL,
    keywords text[] NOT NULL DEFAULT '{}',
    source_conversation_id uuid NOT NULL
        REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
    source_message_ids uuid[] NOT NULL DEFAULT '{}',
    extraction_method varchar(32),
    extraction_job_id uuid,
    confidence numeric(4,3) NOT NULL DEFAULT 0.800,
    importance numeric(4,3) NOT NULL DEFAULT 0.500,
    access_count integer NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    status varchar(32) NOT NULL DEFAULT 'candidate',
    superseded_by_memory_id uuid
        REFERENCES agent.memory_records(memory_id) ON DELETE SET NULL,
    expires_at timestamptz,
    deleted_at timestamptz,
    embedding_model varchar(64),
    embedding_version integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_type_chk
        CHECK (memory_type IN ('fact', 'decision', 'relation', 'context')),
    CONSTRAINT memory_scope_chk
        CHECK (scope = 'conversation'),
    CONSTRAINT memory_status_chk
        CHECK (status IN ('candidate', 'active', 'archived', 'superseded', 'deleted')),
    CONSTRAINT confidence_range_chk
        CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT importance_range_chk
        CHECK (importance >= 0 AND importance <= 1)
);

CREATE INDEX IF NOT EXISTS memory_owner_conversation_status_idx
    ON agent.memory_records (owner_user_id, source_conversation_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS memory_keywords_gin_idx
    ON agent.memory_records USING gin (keywords);

CREATE INDEX IF NOT EXISTS memory_content_fts_idx
    ON agent.memory_records USING gin (to_tsvector('simple', content));

CREATE UNIQUE INDEX IF NOT EXISTS memory_active_dedup_idx
    ON agent.memory_records (
        owner_user_id, source_conversation_id, memory_type, memory_key
    )
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS agent.memory_sources (
    memory_id uuid NOT NULL
        REFERENCES agent.memory_records(memory_id) ON DELETE CASCADE,
    message_id uuid NOT NULL
        REFERENCES agent.messages(message_id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, message_id)
);

COMMIT;
