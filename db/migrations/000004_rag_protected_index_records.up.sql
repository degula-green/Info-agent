BEGIN;

-- ES uses separate display/protected physical indexes. Keep PostgreSQL's
-- index state able to represent both variants without storing chunk bodies.
ALTER TABLE rag.index_records
    DROP CONSTRAINT IF EXISTS index_records_variant_chk;

ALTER TABLE rag.index_records
    ADD CONSTRAINT index_records_variant_chk
    CHECK (content_variant IN ('display', 'protected'));

COMMENT ON COLUMN rag.index_records.content_variant IS
    'ES content variant: display or protected; protected rows map only to the protected index';

COMMIT;
