BEGIN;

ALTER TABLE rag.memory_sources
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE rag.memory_fact_versions
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE rag.memory_node_sources
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS memory_sources_conversation_observed_idx
    ON rag.memory_sources (conversation_key, observed_at);
CREATE INDEX IF NOT EXISTS memory_fact_versions_observed_idx
    ON rag.memory_fact_versions (fact_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS memory_node_sources_observed_idx
    ON rag.memory_node_sources (node_id, observed_at);
CREATE INDEX IF NOT EXISTS memory_nodes_time_idx
    ON rag.memory_nodes (tree_id, time_start, time_end);

COMMIT;
