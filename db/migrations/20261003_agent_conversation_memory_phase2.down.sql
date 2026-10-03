-- Rollback for Phase 2 conversation memory.

BEGIN;

DROP TABLE IF EXISTS agent.memory_sources CASCADE;
DROP TABLE IF EXISTS agent.memory_records CASCADE;

COMMIT;
