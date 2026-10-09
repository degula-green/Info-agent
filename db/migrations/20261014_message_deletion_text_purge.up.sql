ALTER TABLE knowledge.deletion_targets
    DROP CONSTRAINT IF EXISTS deletion_targets_object_chk;

ALTER TABLE knowledge.deletion_targets
    ADD CONSTRAINT deletion_targets_object_chk
        CHECK (object_state IN ('pending', 'deleting', 'deleted', 'skipped', 'failed', 'not_required'));

UPDATE knowledge.deletion_targets target
SET object_state = 'not_required',
    updated_at = now()
FROM knowledge.knowledge_items item
WHERE target.knowledge_item_id = item.id
  AND item.content_type = 'text'
  AND target.object_state = 'pending';

UPDATE knowledge.deletion_requests request
SET purge_after = now(),
    updated_at = now()
WHERE request.status IN ('approved', 'executing')
  AND EXISTS (
      SELECT 1
      FROM knowledge.deletion_targets target
      JOIN knowledge.knowledge_items item ON item.id = target.knowledge_item_id
      WHERE target.deletion_request_id = request.id
        AND item.content_type = 'text'
        AND target.object_state = 'not_required'
  );
