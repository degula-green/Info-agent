BEGIN;

-- Historical WeChat media envelopes were stored as message bodies before the
-- collector learned to separate attachments.  Keep the message/attachment
-- rows and their permissions, but remove only provider XML from media rows so
-- the UI and RAG see the attachment as the sole collected resource.
UPDATE knowledge.messages m
SET normalized_content = NULL,
    content_hash = encode(digest('', 'sha256'), 'hex'),
    content_version = m.content_version + 1,
    classification_status = 'succeeded'
FROM knowledge.conversation_ingestions ci
WHERE ci.id = m.conversation_ingestion_id
  AND ci.platform = 'wechat'
  AND m.message_type IN ('image', 'file', 'video', 'mixed')
  AND EXISTS (SELECT 1 FROM knowledge.attachments a WHERE a.message_id = m.id)
  AND btrim(COALESCE(m.normalized_content, '')) <> ''
  AND btrim(COALESCE(m.normalized_content, '')) ~* '^<\s*(<\?xml[^>]*>\s*)?<msg';

-- A private conversation's display name is the best fallback when a historical
-- sender identity was recorded as a raw wxid.  Do not alter group-member
-- names: a group conversation name must never replace its actual sender.
UPDATE knowledge.messages m
SET sender_display_name = ci.name
FROM knowledge.conversation_ingestions ci
WHERE ci.id = m.conversation_ingestion_id
  AND ci.platform = 'wechat'
  AND ci.conversation_type = 'private'
  AND btrim(COALESCE(ci.name, '')) <> ''
  AND btrim(COALESCE(m.sender_display_name, '')) ~* '^wxid_[a-z0-9_-]+$';

UPDATE knowledge.external_identities ei
SET display_name = ci.name,
    updated_at = CURRENT_TIMESTAMP
FROM knowledge.messages m
JOIN knowledge.conversation_ingestions ci ON ci.id = m.conversation_ingestion_id
WHERE ei.id = m.sender_identity_id
  AND ci.platform = 'wechat'
  AND ci.conversation_type = 'private'
  AND btrim(COALESCE(ci.name, '')) <> ''
  AND btrim(COALESCE(ei.display_name, '')) ~* '^wxid_[a-z0-9_-]+$';

-- The message knowledge item mirrors the display body and must not retain an
-- obsolete XML content reference after the cleanup.
UPDATE knowledge.knowledge_items ki
SET content_ref = 'message:' || ki.source_message_id::text || ':display',
    content_hash = encode(digest('', 'sha256'), 'hex'),
    content_version = GREATEST(ki.content_version, m.content_version),
    updated_at = CURRENT_TIMESTAMP
FROM knowledge.messages m
WHERE ki.source_message_id = m.id
  AND ki.source_attachment_id IS NULL
  AND m.normalized_content IS NULL
  AND m.message_type IN ('image', 'file', 'video', 'mixed')
  AND EXISTS (SELECT 1 FROM knowledge.attachments a WHERE a.message_id = m.id);

COMMIT;
