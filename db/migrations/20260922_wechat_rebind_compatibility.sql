-- Make connector uniqueness lifecycle-aware. Older installations retain the
-- table-level unique constraint from 000001, which incorrectly blocks reuse
-- of a revoked WeChat connector.
DO $$
DECLARE
    duplicate_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT platform, platform_workspace_key, external_account_id
        FROM knowledge.connector_accounts
        WHERE status <> 'revoked'
        GROUP BY platform, platform_workspace_key, external_account_id
        HAVING COUNT(*) > 1
    ) duplicates;
    IF duplicate_count > 0 THEN
        RAISE EXCEPTION
            'connector_accounts has % duplicate active external account group(s); resolve before migration',
            duplicate_count;
    END IF;

    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT owner_user_id, platform
        FROM knowledge.connector_accounts
        WHERE status <> 'revoked'
        GROUP BY owner_user_id, platform
        HAVING COUNT(*) > 1
    ) duplicates;
    IF duplicate_count > 0 THEN
        RAISE EXCEPTION
            'connector_accounts has % duplicate active owner/platform group(s); resolve before migration',
            duplicate_count;
    END IF;
END
$$;

ALTER TABLE knowledge.connector_accounts
    DROP CONSTRAINT IF EXISTS connector_accounts_external_uq;

CREATE UNIQUE INDEX IF NOT EXISTS connector_accounts_owner_platform_active_uq
    ON knowledge.connector_accounts (owner_user_id, platform)
    WHERE status <> 'revoked';

CREATE UNIQUE INDEX IF NOT EXISTS connector_accounts_external_active_uq
    ON knowledge.connector_accounts (platform, platform_workspace_key, external_account_id)
    WHERE status <> 'revoked';
