BEGIN;

CREATE SCHEMA IF NOT EXISTS iam;
CREATE TABLE IF NOT EXISTS iam.authorization_resource_versions (
    resource_id UUID PRIMARY KEY,
    relation_fingerprint CHAR(64) NOT NULL,
    acl_version BIGINT NOT NULL DEFAULT 1 CHECK (acl_version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge.knowledge_bases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_scope VARCHAR(16) NOT NULL,
    base_type VARCHAR(32) NOT NULL,
    name VARCHAR(200) NOT NULL,
    owner_user_id UUID,
    organization_id UUID,
    source_key VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_bases_private_source_runtime_uq
    ON knowledge.knowledge_bases(owner_user_id,base_type,source_key)
    WHERE knowledge_scope='private' AND source_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_bases_org_source_runtime_uq
    ON knowledge.knowledge_bases(organization_id,base_type,source_key)
    WHERE knowledge_scope='organization' AND source_key IS NOT NULL;

-- Runtime installations may have been created from either the complete
-- schema or the connector-only bootstrap. Keep this migration additive.
CREATE TABLE IF NOT EXISTS knowledge.knowledge_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_base_id UUID,
    knowledge_scope VARCHAR(16) NOT NULL,
    access_scope VARCHAR(32) NOT NULL,
    owner_user_id UUID,
    organization_id UUID,
    conversation_ingestion_id UUID NOT NULL REFERENCES knowledge.conversation_ingestions(id),
    source_type VARCHAR(32) NOT NULL DEFAULT 'platform_conversation',
    source_message_id UUID REFERENCES knowledge.messages(id),
    source_attachment_id UUID REFERENCES knowledge.attachments(id),
    content_type VARCHAR(32) NOT NULL,
    content_ref VARCHAR(512) NOT NULL,
    original_content_ref VARCHAR(512),
    content_hash CHAR(64) NOT NULL,
    content_version INTEGER NOT NULL DEFAULT 1,
    content_visibility VARCHAR(16) NOT NULL DEFAULT 'original',
    original_access_required BOOLEAN NOT NULL DEFAULT FALSE,
    security_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    sensitivity VARCHAR(32),
    content_saved BOOLEAN NOT NULL DEFAULT FALSE,
    ownership_ready BOOLEAN NOT NULL DEFAULT FALSE,
    security_ready BOOLEAN NOT NULL DEFAULT FALSE,
    permission_ready BOOLEAN NOT NULL DEFAULT FALSE,
    acl_version BIGINT NOT NULL DEFAULT 0,
    acl_sync_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    processing_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active',
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS knowledge_base_id UUID;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS knowledge_scope VARCHAR(16) NOT NULL DEFAULT 'organization';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS access_scope VARCHAR(32) NOT NULL DEFAULT 'conversation_members';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS owner_user_id UUID;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS organization_id UUID;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS conversation_ingestion_id UUID REFERENCES knowledge.conversation_ingestions(id);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL DEFAULT 'platform_conversation';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS source_message_id UUID REFERENCES knowledge.messages(id);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS source_attachment_id UUID REFERENCES knowledge.attachments(id);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS source_private_item_id UUID REFERENCES knowledge.knowledge_items(id);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS share_request_id VARCHAR(100);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS share_batch_id UUID;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS shared_by_user_id UUID;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS shared_at TIMESTAMPTZ;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_type VARCHAR(32) NOT NULL DEFAULT 'text';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_ref VARCHAR(512) NOT NULL DEFAULT '';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS original_content_ref VARCHAR(512);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_hash CHAR(64) NOT NULL DEFAULT repeat('0', 64);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_visibility VARCHAR(16) NOT NULL DEFAULT 'original';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS original_access_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS security_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS sensitivity VARCHAR(32);
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS content_saved BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS ownership_ready BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS security_ready BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS permission_ready BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS acl_version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS acl_sync_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS processing_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active';
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE knowledge.knowledge_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

DROP INDEX IF EXISTS knowledge.knowledge_items_source_message_runtime_uq;
DROP INDEX IF EXISTS knowledge.knowledge_items_source_attachment_runtime_uq;
CREATE UNIQUE INDEX knowledge_items_source_message_runtime_uq
    ON knowledge.knowledge_items(source_message_id)
    WHERE source_message_id IS NOT NULL AND source_attachment_id IS NULL
      AND source_type <> 'shared_private_item';
CREATE UNIQUE INDEX knowledge_items_source_attachment_runtime_uq
    ON knowledge.knowledge_items(source_attachment_id)
    WHERE source_attachment_id IS NOT NULL AND source_type <> 'shared_private_item';
CREATE INDEX IF NOT EXISTS knowledge_items_permission_pending_runtime_idx
    ON knowledge.knowledge_items(acl_sync_status, updated_at)
    WHERE lifecycle_status = 'active' AND permission_ready = FALSE;
CREATE INDEX IF NOT EXISTS knowledge_items_ready_gate_runtime_idx
    ON knowledge.knowledge_items(processing_status, updated_at)
    WHERE lifecycle_status = 'active';

-- Private conversations are owner-only and do not enter the organization
-- privacy scanner. Make their display content immediately usable.
UPDATE knowledge.messages m
SET normalized_content=pc.content,
    sensitive=FALSE,
    classification_status='succeeded'
FROM knowledge.message_private_content pc,
     knowledge.conversation_ingestions ci
WHERE pc.message_id=m.id
  AND ci.id=m.conversation_ingestion_id
  AND ci.ingestion_scope='private'
  AND btrim(pc.content)<>'';

INSERT INTO knowledge.knowledge_items (
    id,knowledge_base_id,knowledge_scope,access_scope,owner_user_id,organization_id,
    conversation_ingestion_id,source_type,source_message_id,content_type,content_ref,
    original_content_ref,content_hash,content_version,content_visibility,
    original_access_required,security_status,sensitivity,content_saved,ownership_ready,
    security_ready,permission_ready,acl_version,acl_sync_status,processing_status,lifecycle_status
)
SELECT gen_random_uuid(),ci.knowledge_base_id,
       CASE WHEN ci.ingestion_scope='private' THEN 'private' ELSE 'organization' END,
       CASE WHEN ci.ingestion_scope='private' THEN 'owner_only' ELSE 'conversation_members' END,
       CASE WHEN ci.ingestion_scope='private' THEN ci.owner_user_id ELSE NULL END,
       CASE WHEN ci.ingestion_scope='organization' THEN ci.organization_id ELSE NULL END,
       ci.id,
       CASE WHEN ci.ingestion_scope='private' THEN 'private_conversation' ELSE 'platform_conversation' END,
       m.id,'text','message:'||m.id::text||':display','message:'||m.id::text||':original',
       m.content_hash,m.content_version,
       CASE WHEN ci.ingestion_scope='private' THEN 'original' WHEN COALESCE(m.sensitive,FALSE) THEN 'masked' ELSE 'original' END,
       CASE WHEN ci.ingestion_scope='private' THEN FALSE ELSE COALESCE(m.sensitive,FALSE) END,
       CASE WHEN ci.ingestion_scope='private' THEN 'not_required' WHEN m.classification_status='succeeded' THEN 'classified' ELSE 'pending' END,
       CASE WHEN COALESCE(m.sensitive,FALSE) THEN 'restricted' ELSE 'internal' END,
       TRUE,TRUE,(ci.ingestion_scope='private' OR m.classification_status='succeeded'),FALSE,0,'pending','pending','active'
FROM knowledge.messages m
JOIN knowledge.conversation_ingestions ci ON ci.id=m.conversation_ingestion_id
JOIN knowledge.message_private_content pc ON pc.message_id=m.id AND btrim(pc.content)<>''
WHERE NOT EXISTS (
    SELECT 1 FROM knowledge.knowledge_items ki
    WHERE ki.source_message_id=m.id AND ki.source_attachment_id IS NULL
)
ON CONFLICT DO NOTHING;

INSERT INTO knowledge.knowledge_items (
    id,knowledge_base_id,knowledge_scope,access_scope,owner_user_id,organization_id,
    conversation_ingestion_id,source_type,source_message_id,source_attachment_id,
    content_type,content_ref,original_content_ref,content_hash,content_version,
    content_visibility,original_access_required,security_status,sensitivity,
    content_saved,ownership_ready,security_ready,permission_ready,acl_version,
    acl_sync_status,processing_status,lifecycle_status
)
SELECT gen_random_uuid(),ci.knowledge_base_id,
       CASE WHEN ci.ingestion_scope='private' THEN 'private' ELSE 'organization' END,
       CASE WHEN ci.ingestion_scope='private' THEN 'owner_only' ELSE 'conversation_members' END,
       CASE WHEN ci.ingestion_scope='private' THEN ci.owner_user_id ELSE NULL END,
       CASE WHEN ci.ingestion_scope='organization' THEN ci.organization_id ELSE NULL END,
       ci.id,
       CASE WHEN ci.ingestion_scope='private' THEN 'private_conversation' ELSE 'platform_conversation' END,
       a.message_id,a.id,
       CASE WHEN COALESCE(a.mime_type,'') LIKE 'image/%' THEN 'image' ELSE 'file' END,
       COALESCE(NULLIF(a.object_ref,''),'attachment:'||a.id::text),
       COALESCE(NULLIF(a.object_ref,''),'attachment:'||a.id::text),
       COALESCE(a.content_hash,encode(digest(a.file_name,'sha256'),'hex')),
       a.content_version,
       CASE WHEN COALESCE(a.sensitive,FALSE) THEN 'metadata_only' ELSE 'original' END,
       COALESCE(a.sensitive,FALSE),CASE WHEN ci.ingestion_scope='private' THEN 'not_required' ELSE 'classified' END,
       CASE WHEN COALESCE(a.sensitive,FALSE) THEN 'restricted' ELSE 'internal' END,
       a.content_status='ready',TRUE,TRUE,FALSE,0,'pending','pending','active'
FROM knowledge.attachments a
JOIN knowledge.conversation_ingestions ci ON ci.id=a.conversation_ingestion_id
WHERE a.message_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM knowledge.knowledge_items ki WHERE ki.source_attachment_id=a.id
  )
ON CONFLICT DO NOTHING;

-- Allow the unified transport type set on both schema generations.
ALTER TABLE knowledge.messages DROP CONSTRAINT IF EXISTS messages_message_type_check;
ALTER TABLE knowledge.messages DROP CONSTRAINT IF EXISTS messages_type_chk;
ALTER TABLE knowledge.messages ADD CONSTRAINT messages_message_type_check
    CHECK (message_type IN ('text', 'image', 'file', 'video', 'mixed', 'system'));

ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE knowledge.outbox_events ADD COLUMN IF NOT EXISTS publish_attempts INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_knowledge_ready_version_uq
    ON knowledge.outbox_events(aggregate_type, aggregate_id, event_type, event_version)
    WHERE event_type = 'knowledge.ready';
CREATE INDEX IF NOT EXISTS outbox_knowledge_ready_pending_idx
    ON knowledge.outbox_events(available_at, created_at)
    WHERE event_type = 'knowledge.ready' AND published_at IS NULL;

-- Older publishers treated every Outbox row as externally publishable. Reset
-- internal permission requests so the permission worker owns their lifecycle.
UPDATE knowledge.outbox_events oe
SET status='pending',published_at=NULL,last_error=NULL,available_at=CURRENT_TIMESTAMP
FROM knowledge.knowledge_items ki
WHERE oe.aggregate_id=ki.id
  AND oe.event_type='permission.sync.requested'
  AND ki.permission_ready=FALSE;

UPDATE knowledge.outbox_events oe
SET status='published',published_at=COALESCE(oe.published_at,CURRENT_TIMESTAMP),last_error=NULL
FROM knowledge.knowledge_items ki
WHERE oe.aggregate_id=ki.id
  AND oe.event_type='permission.sync.requested'
  AND ki.permission_ready=TRUE;

INSERT INTO knowledge.outbox_events (
    id,aggregate_type,aggregate_id,event_type,event_version,schema_version,
    organization_id,trace_id,payload,status,retry_count,available_at
)
SELECT gen_random_uuid(),'knowledge_item',ki.id,'permission.sync.requested',ki.content_version,1,
       ki.organization_id,'migration:'||ki.id::text,
       jsonb_build_object('knowledge_item_id',ki.id::text,'content_version',ki.content_version),
       'pending',0,CURRENT_TIMESTAMP
FROM knowledge.knowledge_items ki
WHERE ki.lifecycle_status='active' AND ki.permission_ready=FALSE
  AND NOT EXISTS (
      SELECT 1 FROM knowledge.outbox_events oe
      WHERE oe.aggregate_id=ki.id AND oe.event_type='permission.sync.requested'
  )
ON CONFLICT DO NOTHING;

COMMIT;
