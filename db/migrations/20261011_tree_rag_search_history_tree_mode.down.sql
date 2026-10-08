BEGIN;

ALTER TABLE rag_mvp.search_history
    DROP CONSTRAINT IF EXISTS search_history_tree_mode_chk;
ALTER TABLE rag_mvp.search_history
    ADD CONSTRAINT search_history_tree_mode_chk CHECK (
        tree_mode IS NULL OR tree_mode IN ('off', 'shadow', 'boost')
    );

COMMIT;
