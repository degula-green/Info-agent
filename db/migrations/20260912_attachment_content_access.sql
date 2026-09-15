BEGIN;

-- A conversation member can already access the containing conversation. The
-- previous ingestion rule incorrectly marked every organization-scoped
-- attachment as requiring separate approval, including ordinary files.
UPDATE knowledge.attachments
SET content_access_required = FALSE
WHERE COALESCE(sensitive, FALSE) = FALSE
  AND content_access_required = TRUE;

COMMIT;
