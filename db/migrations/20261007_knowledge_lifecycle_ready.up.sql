-- The ready pipeline writes lifecycle_status='ready' for a finalized local
-- upload (FinalizeLocalUpload -> TryMarkKnowledgeReady), but the original
-- constraint only allowed 'active'. Widening it is what lets that transaction
-- commit at all: while it was rejected the whole ready write rolled back,
-- content_saved stayed FALSE, no knowledge.ready event was EVER queued, and a
-- local-library upload could never be indexed or found by content search.
--
-- 'deleting'/'deleted'/'purged' are kept in the allowed set because runtime
-- installations have been observed with them; dropping a value would make the
-- constraint reject rows a deployment already stores.
ALTER TABLE knowledge.knowledge_items
    DROP CONSTRAINT IF EXISTS knowledge_items_lifecycle_chk;

ALTER TABLE knowledge.knowledge_items
    ADD CONSTRAINT knowledge_items_lifecycle_chk
    CHECK (lifecycle_status IN ('active', 'ready', 'deleting', 'deleted', 'purged'));
