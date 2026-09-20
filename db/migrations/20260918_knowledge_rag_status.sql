BEGIN;

-- The RAG outbox already exists in the initial schema. Extend its allow-list
-- for HTTP status callbacks without replacing the table or historical rows.
ALTER TABLE rag.outbox_events
    DROP CONSTRAINT IF EXISTS rag_outbox_events_type_chk;
ALTER TABLE rag.outbox_events
    ADD CONSTRAINT rag_outbox_events_type_chk
    CHECK (event_type IN (
        'document.extracted', 'processing.completed', 'processing.failed',
        'knowledge.rag.processing', 'knowledge.rag.succeeded', 'knowledge.rag.failed'
    ));

ALTER TABLE knowledge.knowledge_items
    ADD COLUMN IF NOT EXISTS rag_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS rag_source_event_id UUID,
    ADD COLUMN IF NOT EXISTS rag_job_id UUID,
    ADD COLUMN IF NOT EXISTS rag_content_version INTEGER,
    ADD COLUMN IF NOT EXISTS rag_acl_version BIGINT,
    ADD COLUMN IF NOT EXISTS rag_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rag_finished_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rag_last_error TEXT,
    ADD COLUMN IF NOT EXISTS rag_result JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'knowledge_items_rag_status_chk'
          AND conrelid = 'knowledge.knowledge_items'::regclass
    ) THEN
        ALTER TABLE knowledge.knowledge_items
            ADD CONSTRAINT knowledge_items_rag_status_chk
            CHECK (rag_status IN ('pending', 'processing', 'succeeded', 'failed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS knowledge_items_rag_status_idx
    ON knowledge.knowledge_items (rag_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS knowledge_items_rag_job_idx
    ON knowledge.knowledge_items (rag_job_id)
    WHERE rag_job_id IS NOT NULL;

ALTER TABLE rag.qa_conversations
    ADD COLUMN IF NOT EXISTS retrieval_mode VARCHAR(16) NOT NULL DEFAULT 'quick',
    ADD COLUMN IF NOT EXISTS knowledge_base_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'qa_conversations_retrieval_mode_chk'
          AND conrelid = 'rag.qa_conversations'::regclass
    ) THEN
        ALTER TABLE rag.qa_conversations
            ADD CONSTRAINT qa_conversations_retrieval_mode_chk
            CHECK (retrieval_mode IN ('quick', 'deep'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS qa_conversations_active_updated_idx
    ON rag.qa_conversations (user_id, updated_at DESC)
    WHERE status = 'active' AND deleted_at IS NULL;

COMMIT;
