-- Keep contact list aggregation and conversation detail reads index-backed as
-- message history grows. All statements are safe to re-run.
CREATE INDEX IF NOT EXISTS messages_conversation_sender_idx
    ON knowledge.messages (conversation_ingestion_id, sender_identity_id);

CREATE INDEX IF NOT EXISTS contact_relations_owner_identity_active_idx
    ON knowledge.contact_relations (owner_user_id, external_identity_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS conversation_memberships_identity_conversation_active_idx
    ON knowledge.conversation_memberships (external_identity_id, conversation_ingestion_id)
    WHERE status = 'active';
