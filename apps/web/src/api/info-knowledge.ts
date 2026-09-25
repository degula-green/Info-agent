import { ApiError, knowledgeContentURL, knowledgeFetch, knowledgeHeaders, knowledgeRequest } from './http.ts'

export type ConnectorPlatform = 'feishu' | 'wecom' | 'wechat'
export type ConnectorStatus = 'unbound' | 'active' | 'expired' | 'revoked' | 'error' | 'reauthorization_required'

export interface ConnectorDTO {
  platform: ConnectorPlatform
  display_name: string
  bound: boolean
  status: ConnectorStatus | string
  availability: 'available' | 'unavailable'
  account_name?: string
  account_id?: string
  default_organization_id?: string
  cleanup_pending?: boolean
  last_error?: string | null
  agent_online?: boolean
  last_heartbeat_at?: string | null
}
export type Connector = ConnectorDTO

export function connectorCatalog(): ConnectorDTO[] {
  return [
    { platform: 'feishu', display_name: '飞书', bound: false, status: 'unbound', availability: 'available' },
    { platform: 'wecom', display_name: '企业微信', bound: false, status: 'unbound', availability: 'unavailable' },
    { platform: 'wechat', display_name: '个人微信', bound: false, status: 'unbound', availability: 'available' },
  ]
}

export interface AvailableConversationDTO {
  external_id: string
  name: string
  conversation_type: 'private' | 'group' | string
  member_count: number
  members?: Array<{ external_user_id: string; display_name?: string; member_role?: string }>
  last_seen_at?: string | null
  message_count?: number
  attachment_count?: number
  metadata?: Record<string, unknown>
  attached_conversation_id?: string
  current_user_collector: boolean
}

export interface DiscoveryDTO {
  discovery_id: string
  connector_id: string
  platform: ConnectorPlatform
  expires_at: string
  conversations: AvailableConversationDTO[]
}

export type KnowledgeScope = 'organization' | 'personal'
export type KnowledgeLibraryBaseType =
  | 'organization_files'
  | 'organization_conversation'
  | 'organization_private_shared'
  | 'private_conversation'
  | 'private_local'

export interface KnowledgeLibraryDTO {
  id: string
  scope: KnowledgeScope | string
  base_type: KnowledgeLibraryBaseType | string
  name: string
  status: string
  item_count: number
  file_count: number
  conversation_count: number
  message_count: number
  shared_item_count: number
  can_upload: boolean
  updated_at: string
}

export interface KnowledgeLibraryItemDTO {
  id: string
  library_id: string
  kind: 'conversation' | 'message' | 'file' | string
  title: string
  excerpt?: string
  platform?: ConnectorPlatform | string
  conversation_id?: string
  external_conversation_id?: string
  conversation_type?: 'private' | 'group' | string
  conversation_name?: string
  collection_status?: 'not_started' | 'collecting' | 'paused' | 'detached' | 'missing' | 'error' | string
  source_type: string
  source_message_id?: string
  source_attachment_id?: string
  content_type?: string
  content_visibility?: string
  access_scope?: string
  processing_status?: string
  content_status?: string
  file_name?: string
  mime_type?: string
  size_bytes?: number
  message_count?: number
  attachment_count?: number
  member_count?: number
  sent_at?: string | null
  created_at: string
  updated_at: string
  shared_at?: string | null
  share_batch_id?: string
  can_view: boolean
  can_download: boolean
  content_access_required: boolean
  rag_status?: string
  rag_content_version?: number
  rag_acl_version?: number
  rag_last_error?: string | null
  searchable?: boolean
}

export interface LocalUploadTaskDTO {
  request_id: string
  upload_destination: 'private_local_library' | 'organization_file_library' | string
  file_name: string
  mime_type: string
  size_bytes: number
  content_hash: string
  upload_status: string
  processing_status: string
  attachment_id?: string
  resource_id?: string
  organization_id?: string
  content_status?: string
  created_at?: string
  updated_at?: string
}

export interface CollectorDTO {
  id: string
  conversation_id: string
  collector_user_id: string
  collector_role: 'primary' | 'supplemental' | string
  status: 'active' | 'unavailable' | 'removed' | string
  last_cursor?: string
  last_success_at?: string | null
  last_attempt_at?: string | null
  next_poll_at?: string | null
  consecutive_failures: number
  last_error?: string | null
  joined_at: string
  removed_at?: string | null
  agent_online?: boolean
  last_heartbeat_at?: string | null
}

export interface AttachmentDTO {
  id: string
  conversation_id: string
  message_id?: string
  external_attachment_id: string
  file_name: string
  mime_type?: string
  size_bytes: number
  content_hash?: string
  content_version: number
  content_status: 'pending' | 'ready' | 'failed' | string
  access_scope: string
  content_access_required: boolean
  preview_capability?: string
  last_error?: string | null
  rag_status?: 'not_enqueued' | 'pending' | 'processing' | 'succeeded' | 'failed' | string
  rag_content_version?: number
  rag_acl_version?: number
  rag_last_error?: string | null
  rag_finished_at?: string | null
  searchable?: boolean
  created_at: string
  updated_at: string
}

export interface PrivateAccessRequestDTO {
  id: string
  requester_user_id: string
  share_reference_id: string
  resource_id: string
  resource_type: 'message' | 'attachment' | string
  requested_action: 'view' | 'download' | string
  reason?: string
  status: 'pending' | 'approved' | 'rejected' | 'expired' | 'revoked' | string
  reviewed_by_user_id?: string
  review_note?: string
  created_at: string
  reviewed_at?: string | null
}

export interface ConversationTimelineItemDTO {
  kind: 'message' | 'attachment'
  collected_at: string
  message?: MessageDTO
  attachment?: AttachmentDTO
  sender_identity_id?: string
  sender_display_name?: string
  sent_at?: string | null
}

export interface ConversationTimelinePageDTO {
  items: ConversationTimelineItemDTO[]
  has_more: boolean
  next_cursor?: string
}

export interface MessageDTO {
  id: string
  conversation_id: string
  external_message_id: string
  sender_identity_id?: string
  sender_display_name?: string
  message_type: string
  content?: string
  normalized_content_ref?: string
  content_hash: string
  content_version: number
  sent_at: string
  collected_at: string
  lifecycle_status: string
  vector_status?: string
  attachments?: AttachmentDTO[]
  created_at: string
}

export interface ConversationDTO {
  id: string
  platform: ConnectorPlatform
  external_conversation_id: string
  conversation_type: 'private' | 'group' | string
  name: string
  avatar_url?: string
  knowledge_base_id?: string
  ingestion_scope: 'private' | 'organization' | string
  organization_id?: string
  requested_start_at?: string | null
  effective_start_at?: string | null
  status: 'active' | 'paused' | 'detached' | 'error' | string
  pause_reason?: string
  last_synced_at?: string | null
  detached_at?: string | null
  created_at: string
  updated_at: string
  message_count: number
  attachment_count: number
  collectors?: CollectorDTO[]
  memberships?: Array<{ id: string; external_identity_id?: string; external_user_id: string; display_name?: string; member_role?: string; status: string }>
  member_count?: number
}

export interface ConversationDetail {
  conversation: ConversationDTO
  timeline: ConversationTimelinePageDTO
}

export async function getConnectors() {
  const body = await knowledgeRequest<{ items: ConnectorDTO[] }>('/connectors')
  return body.items || []
}

export async function getFeishuAuthorizeURL(intent: 'bind' | 'rebind' = 'bind') {
  const body = await knowledgeRequest<{ authorize_url: string }>('/connectors/feishu/authorize', { method: 'POST', body: JSON.stringify({ intent }) })
  return body.authorize_url
}

export async function bindWechat(wxid: string, dbDir: string, rebind = false) {
  return knowledgeRequest<Record<string, any>>(`/connectors/wechat/${rebind ? 'rebind' : 'bind'}`, { method: 'POST', body: JSON.stringify({ wxid, db_dir: dbDir }) })
}
export async function getWechatStatus() { return knowledgeRequest<Record<string, any>>('/connectors/wechat/status') }
export async function stopWechat() { return knowledgeRequest<{ status: string }>('/connectors/wechat/stop', { method: 'POST' }) }
export async function getWechatConversations() { return knowledgeRequest<Record<string, any>>('/connectors/wechat/local-conversations') }
export async function getWechatConfig() { return knowledgeRequest<Record<string, any>>('/connectors/wechat/config') }
export async function saveWechatConfig(value: Record<string, any>) { return knowledgeRequest<Record<string, any>>('/connectors/wechat/config', { method: 'PUT', body: JSON.stringify(value) }) }

export async function unbindConnector(platform: ConnectorPlatform) {
  return knowledgeRequest<{ status: string }>(`/connectors/${encodeURIComponent(platform)}`, { method: 'DELETE' })
}


export async function discoverConversations(platform: ConnectorPlatform) {
  return knowledgeRequest<DiscoveryDTO>(`/connectors/${encodeURIComponent(platform)}/conversations/discover`)
}

export async function discoverConversationsByType(platform: ConnectorPlatform, conversationType: 'group' | 'private') {
  const path = conversationType === 'group' ? 'group-conversations' : 'private-conversations'
  return knowledgeRequest<DiscoveryDTO>(`/connectors/${encodeURIComponent(platform)}/${path}/discover`)
}

export async function listConversations(platform: ConnectorPlatform) {
  const body = await knowledgeRequest<{ items: ConversationDTO[] }>(`/connectors/${encodeURIComponent(platform)}/conversations`)
  return body.items || []
}

export async function attachConversation(input: { platform: ConnectorPlatform; externalConversationID: string; conversationType: string; name?: string; discoveryID: string; organizationID?: string; requestedStartAt?: string | null }) {
  return knowledgeRequest<ConversationDTO>('/conversations/attach', {
    method: 'POST',
    body: JSON.stringify({
      platform: input.platform,
      external_conversation_id: input.externalConversationID,
      conversation_type: input.conversationType,
      name: input.name || '',
      discovery_id: input.discoveryID,
      organization_id: input.organizationID || '',
      requested_start_at: input.requestedStartAt || '',
    }),
  })
}

export async function addConversationCollector(conversationID: string) {
  return knowledgeRequest<CollectorDTO>(`/conversations/${encodeURIComponent(conversationID)}/collectors`, { method: 'POST' })
}

export async function removeConversationCollector(conversationID: string, collectorID: string) {
  return knowledgeRequest<{ status: string }>(`/conversations/${encodeURIComponent(conversationID)}/collectors/${encodeURIComponent(collectorID)}`, { method: 'DELETE' })
}

export async function setConversationStatus(conversationID: string, status: 'pause' | 'resume') {
  return knowledgeRequest<{ status: string }>(`/conversations/${encodeURIComponent(conversationID)}/${status}`, { method: 'POST' })
}

export async function getConversationDetail(conversationID: string, limit = 50): Promise<ConversationDetail> {
  const encodedID = encodeURIComponent(conversationID)
  const [conversation, timeline] = await Promise.all([
    knowledgeRequest<ConversationDTO>(`/conversations/${encodedID}`),
    knowledgeRequest<ConversationTimelinePageDTO>(`/conversations/${encodedID}/timeline?limit=${limit}`),
  ])
  return { conversation, timeline: { ...timeline, items: timeline.items || [] } }
}

export async function getConversationTimeline(conversationID: string, cursor: string, limit = 50): Promise<ConversationTimelinePageDTO> {
  const query = new URLSearchParams({ limit: String(limit), before: cursor })
  const page = await knowledgeRequest<ConversationTimelinePageDTO>(`/conversations/${encodeURIComponent(conversationID)}/timeline?${query}`)
  return { ...page, items: page.items || [] }
}

export async function attachConversationByType(input: { type: 'group' | 'private'; platform: ConnectorPlatform; externalConversationID: string; conversationType: 'group' | 'private'; name?: string; discoveryID: string; organizationID?: string; requestedStartAt?: string | null }) {
  const path = input.type === 'group' ? '/conversations/group/attach' : '/conversations/private/attach'
  return knowledgeRequest<ConversationDTO>(path, {
    method: 'POST',
    body: JSON.stringify({
      platform: input.platform,
      external_conversation_id: input.externalConversationID,
      conversation_type: input.conversationType,
      name: input.name || '',
      discovery_id: input.discoveryID,
      organization_id: input.organizationID || '',
      requested_start_at: input.requestedStartAt || '',
    }),
  })
}

export async function getKnowledgeLibraries() {
  const body = await knowledgeRequest<{ items: KnowledgeLibraryDTO[] }>('/knowledge/libraries')
  return body.items || []
}

export async function getKnowledgeLibraryItems(libraryID: string, options: { kind?: string; platform?: string; query?: string; limit?: number } = {}) {
  const params = new URLSearchParams()
  if (options.kind) params.set('kind', options.kind)
  if (options.platform) params.set('platform', options.platform)
  if (options.query) params.set('q', options.query)
  if (options.limit) params.set('limit', String(options.limit))
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const body = await knowledgeRequest<{ items: KnowledgeLibraryItemDTO[] }>(`/knowledge/libraries/${encodeURIComponent(libraryID)}/items${suffix}`)
  return body.items || []
}

export async function createLocalUploadTask(input: { requestID: string; traceID?: string; uploadDestination: 'private_local_library' | 'organization_file_library'; fileName: string; mimeType: string; sizeBytes: number; contentHash: string }) {
  return knowledgeRequest<LocalUploadTaskDTO>('/attachments/upload-tasks', {
    method: 'POST',
    body: JSON.stringify({
      request_id: input.requestID,
      trace_id: input.traceID || input.requestID,
      upload_destination: input.uploadDestination,
      file_name: input.fileName,
      mime_type: input.mimeType,
      size_bytes: input.sizeBytes,
      content_hash: input.contentHash,
    }),
  })
}

export async function uploadLocalContent(requestID: string, content: Blob) {
  return knowledgeRequest<LocalUploadTaskDTO>(`/attachments/upload-tasks/${encodeURIComponent(requestID)}/content`, {
    method: 'PUT',
    headers: { 'Content-Type': content.type || 'application/octet-stream' },
    body: content,
  })
}

export async function getLocalUploadTask(requestID: string) {
  return knowledgeRequest<LocalUploadTaskDTO>(`/attachments/upload-tasks/${encodeURIComponent(requestID)}`)
}

export async function sharePrivateResources(input: { requestID: string; privateConversationID: string; messageIDs?: string[]; attachmentIDs?: string[] }) {
  return knowledgeRequest<Record<string, any>>('/private/shares', {
    method: 'POST',
    body: JSON.stringify({
      request_id: input.requestID,
      private_conversation_id: input.privateConversationID,
      message_ids: input.messageIDs || [],
      attachment_ids: input.attachmentIDs || [],
    }),
  })
}

export async function listPrivateAccessRequests(scope: 'mine' | 'inbox') {
  const body = await knowledgeRequest<{ items: PrivateAccessRequestDTO[] }>(`/private-access-requests?scope=${scope}`)
  return body.items || []
}

export async function createPrivateAccessRequest(input: { shareReferenceID: string; resourceID: string; resourceType: 'message' | 'attachment'; requestedAction: 'view' | 'download'; reason?: string }) {
  return knowledgeRequest<PrivateAccessRequestDTO>('/private-access-requests', {
    method: 'POST',
    body: JSON.stringify({
      share_reference_id: input.shareReferenceID,
      resource_id: input.resourceID,
      resource_type: input.resourceType,
      requested_action: input.requestedAction,
      reason: input.reason || '',
    }),
  })
}

export async function approvePrivateAccessRequest(id: string, note = '') {
  return knowledgeRequest<PrivateAccessRequestDTO>(`/private-share-requests/${encodeURIComponent(id)}/approve`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })
}

export async function rejectPrivateAccessRequest(id: string, note = '') {
  return knowledgeRequest<PrivateAccessRequestDTO>(`/private-share-requests/${encodeURIComponent(id)}/reject`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })
}

export async function getKnowledgeAttachmentContent(id: string, download = false) {
  const action = download ? '?action=download' : ''
  const response = await knowledgeFetch(knowledgeContentURL(`/attachments/${encodeURIComponent(id)}/content${action}`), {
    headers: knowledgeHeaders(undefined, download ? 'application/octet-stream' : '*/*'),
  })
  if (!response.ok) {
    let error: any = {}
    try { error = await response.json() } catch { /* empty error body */ }
    throw new ApiError(error.message || '附件内容暂不可用', error.code || 'attachment_not_ready', response.status, Boolean(error.retryable))
  }
  return response.blob()
}

export async function listKnowledgeConversationAttachments(conversationID: string) {
  return knowledgeRequest<{ items: AttachmentDTO[] }>(`/conversations/${encodeURIComponent(conversationID)}/attachments`)
}
