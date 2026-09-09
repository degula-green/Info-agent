BEGIN;

-- This migration is intentionally additive. It assumes 000001 has already run.
-- Hashes are stored as 64 lowercase hexadecimal characters without a prefix.

-- Fail before changing the schema when existing data cannot satisfy the new
-- hash contract. The application must clean such rows before retrying.
DO $$
DECLARE
    invalid_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO invalid_count
    FROM knowledge.messages
    WHERE content_hash IS NULL
       OR content_hash !~ '^[0-9a-f]{64}$';
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
            'knowledge.messages.content_hash has % invalid row(s); clean data before migration',
            invalid_count;
    END IF;

    SELECT COUNT(*) INTO invalid_count
    FROM knowledge.attachments
    WHERE content_hash IS NULL
       OR content_hash !~ '^[0-9a-f]{64}$';
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
            'knowledge.attachments.content_hash has % invalid row(s); clean data before migration',
            invalid_count;
    END IF;

    SELECT COUNT(*) INTO invalid_count
    FROM knowledge.knowledge_items
    WHERE content_hash IS NULL
       OR content_hash !~ '^[0-9a-f]{64}$';
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
            'knowledge.knowledge_items.content_hash has % invalid row(s); clean data before migration',
            invalid_count;
    END IF;

    SELECT COUNT(*) INTO invalid_count
    FROM rag.processing_jobs
    WHERE parsed_content_hash IS NOT NULL
      AND parsed_content_hash !~ '^[0-9a-f]{64}$';
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
            'rag.processing_jobs.parsed_content_hash has % invalid row(s); clean data before migration',
            invalid_count;
    END IF;
END
$$;

ALTER TABLE knowledge.attachments
    ADD COLUMN external_attachment_id VARCHAR(255);

-- A platform attachment is identified by its source message and platform ID.
-- The message check prevents an external ID from being attached to a local
-- upload, for which the request_id idempotency key is used instead.
ALTER TABLE knowledge.attachments
    ADD CONSTRAINT attachments_external_source_chk
    CHECK (external_attachment_id IS NULL OR message_id IS NOT NULL)
    NOT VALID;

DO $$
DECLARE
    duplicate_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT message_id, external_attachment_id
        FROM knowledge.attachments
        WHERE external_attachment_id IS NOT NULL
        GROUP BY message_id, external_attachment_id
        HAVING COUNT(*) > 1
    ) duplicates;

    IF duplicate_count > 0 THEN
        RAISE EXCEPTION
            'knowledge.attachments has % duplicate (message_id, external_attachment_id) group(s)',
            duplicate_count;
    END IF;
END
$$;

-- Older databases may have named this index either as a constraint or as a
-- standalone unique index. Drop only single-column unique indexes on object_ref.
DO $$
DECLARE
    item RECORD;
BEGIN
    FOR item IN
        SELECT
            ns.nspname AS schema_name,
            idxrel.relname AS index_name,
            con.conname AS constraint_name
        FROM pg_index idx
        JOIN pg_class tbl ON tbl.oid = idx.indrelid
        JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
        JOIN pg_class idxrel ON idxrel.oid = idx.indexrelid
        LEFT JOIN pg_constraint con ON con.conindid = idx.indexrelid
        JOIN pg_attribute object_attr
          ON object_attr.attrelid = tbl.oid
         AND object_attr.attname = 'object_ref'
         AND object_attr.attnum = ANY (idx.indkey)
        WHERE ns.nspname = 'knowledge'
          AND tbl.relname = 'attachments'
          AND idx.indisunique
          AND idx.indnkeyatts = 1
    LOOP
        IF item.constraint_name IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE %I.%I DROP CONSTRAINT %I',
                item.schema_name, 'attachments', item.constraint_name
            );
        ELSE
            EXECUTE format(
                'DROP INDEX %I.%I', item.schema_name, item.index_name
            );
        END IF;
    END LOOP;
END
$$;

-- Keep a non-unique lookup index because many logical attachments may share an
-- immutable physical object.
CREATE INDEX IF NOT EXISTS attachments_object_ref_idx
    ON knowledge.attachments (object_ref);

CREATE UNIQUE INDEX uq_attachments_platform_source
    ON knowledge.attachments (message_id, external_attachment_id)
    WHERE external_attachment_id IS NOT NULL;

CREATE INDEX attachments_external_attachment_idx
    ON knowledge.attachments (external_attachment_id)
    WHERE external_attachment_id IS NOT NULL;

ALTER TABLE knowledge.attachments
    VALIDATE CONSTRAINT attachments_external_source_chk;

ALTER TABLE knowledge.messages
    ADD CONSTRAINT messages_content_hash_chk
    CHECK (content_hash ~ '^[0-9a-f]{64}$')
    NOT VALID;
ALTER TABLE knowledge.attachments
    ADD CONSTRAINT attachments_content_hash_chk
    CHECK (content_hash ~ '^[0-9a-f]{64}$')
    NOT VALID;
ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_content_hash_chk
    CHECK (content_hash ~ '^[0-9a-f]{64}$')
    NOT VALID;
ALTER TABLE rag.processing_jobs
    ADD CONSTRAINT processing_jobs_parsed_content_hash_chk
    CHECK (
        parsed_content_hash IS NULL
        OR parsed_content_hash ~ '^[0-9a-f]{64}$'
    )
    NOT VALID;

ALTER TABLE knowledge.messages
    VALIDATE CONSTRAINT messages_content_hash_chk;
ALTER TABLE knowledge.attachments
    VALIDATE CONSTRAINT attachments_content_hash_chk;
ALTER TABLE knowledge.knowledge_items
    VALIDATE CONSTRAINT knowledge_items_content_hash_chk;
ALTER TABLE rag.processing_jobs
    VALIDATE CONSTRAINT processing_jobs_parsed_content_hash_chk;

-- RAG owns this outbox. Knowledge must continue writing only to
-- knowledge.outbox_events; the two tables have separate publisher ownership.
CREATE TABLE rag.outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    event_version BIGINT NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1,
    organization_id UUID,
    trace_id VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT rag_outbox_events_aggregate_event_uq
        UNIQUE (aggregate_type, aggregate_id, event_type, event_version),
    CONSTRAINT rag_outbox_events_type_chk
        CHECK (event_type IN (
            'document.extracted', 'processing.completed', 'processing.failed'
        )),
    CONSTRAINT rag_outbox_events_event_version_chk CHECK (event_version >= 1),
    CONSTRAINT rag_outbox_events_schema_version_chk CHECK (schema_version >= 1),
    CONSTRAINT rag_outbox_events_status_chk
        CHECK (status IN ('pending', 'publishing', 'published', 'failed')),
    CONSTRAINT rag_outbox_events_retry_count_chk CHECK (retry_count >= 0)
);

CREATE INDEX rag_outbox_events_aggregate_idx
    ON rag.outbox_events (aggregate_type, aggregate_id);
CREATE INDEX rag_outbox_events_type_idx
    ON rag.outbox_events (event_type);
CREATE INDEX rag_outbox_events_org_idx
    ON rag.outbox_events (organization_id);
CREATE INDEX rag_outbox_events_trace_idx
    ON rag.outbox_events (trace_id);
CREATE INDEX rag_outbox_events_publish_idx
    ON rag.outbox_events (status, available_at);
CREATE INDEX rag_outbox_events_created_at_idx
    ON rag.outbox_events (created_at);

-- One record represents one complete share request. The service creates the
-- share_batch_id and request_fingerprint from the private conversation ID plus
-- the canonical sorted message ID list, then performs the sharing work in a
-- separate transaction.
CREATE TABLE knowledge.private_share_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_user_id UUID NOT NULL,
    request_id VARCHAR(100) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    private_conversation_id UUID NOT NULL
        REFERENCES knowledge.conversation_ingestions (id),
    organization_id UUID NOT NULL,
    share_batch_id UUID NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'processing',
    shared_message_count INTEGER NOT NULL DEFAULT 0,
    shared_attachment_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT private_share_requests_request_uq
        UNIQUE (requester_user_id, request_id),
    CONSTRAINT private_share_requests_fingerprint_chk
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT private_share_requests_status_chk
        CHECK (status IN ('processing', 'completed', 'failed')),
    CONSTRAINT private_share_requests_message_count_chk
        CHECK (shared_message_count >= 0),
    CONSTRAINT private_share_requests_attachment_count_chk
        CHECK (shared_attachment_count >= 0)
);

CREATE INDEX private_share_requests_status_idx
    ON knowledge.private_share_requests (status, updated_at);
CREATE INDEX private_share_requests_conversation_idx
    ON knowledge.private_share_requests (private_conversation_id);

COMMIT;
