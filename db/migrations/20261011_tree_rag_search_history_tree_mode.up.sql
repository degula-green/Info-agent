-- search_history.tree_mode still only allowed off/shadow/boost, but the tree
-- mode was renamed to off/shadow/tree. The insert therefore failed with a check
-- violation while retrieval itself kept working, so the observability data was
-- silently missing for every tree-mode search.
--
-- 'boost' is kept in the allowed set so pre-existing rows stay valid.
BEGIN;

ALTER TABLE rag_mvp.search_history
    DROP CONSTRAINT IF EXISTS search_history_tree_mode_chk;
ALTER TABLE rag_mvp.search_history
    ADD CONSTRAINT search_history_tree_mode_chk CHECK (
        tree_mode IS NULL OR tree_mode IN ('off', 'shadow', 'tree', 'boost')
    );

COMMIT;
