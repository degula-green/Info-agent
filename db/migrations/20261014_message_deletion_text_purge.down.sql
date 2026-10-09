UPDATE knowledge.deletion_targets
SET object_state = 'skipped',
    updated_at = now()
WHERE object_state = 'not_required';

ALTER TABLE knowledge.deletion_targets
    DROP CONSTRAINT IF EXISTS deletion_targets_object_chk;

ALTER TABLE knowledge.deletion_targets
    ADD CONSTRAINT deletion_targets_object_chk
        CHECK (object_state IN ('pending', 'deleting', 'deleted', 'skipped', 'failed'));
