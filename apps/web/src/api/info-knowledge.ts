import { ApiError, knowledgeContentURL, knowledgeHeaders, knowledgeRequest } from './http.ts'

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
  created_at: string
  updated_at: string
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
  collectors?: CollectorDTO[]
  memberships?: Array<{ id: string; external_user_id: string; display_name?: string; member_role?: string; status: string }>
}

export interface ConversationDetail {
  conversation: ConversationDTO
  messages: MessageDTO[]
  attachments: AttachmentDTO[]
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
export async function getWechatConversations() { return knowledgeRequest<Record<string, any>>('/connectors/wechat/conversations') }
export async function getWechatConfig() { return knowledgeRequest<Record<string, any>>('/connectors/wechat/config') }
export async function saveWechatConfig(value: Record<string, any>) { return knowledgeRequest<Record<string, any>>('/connectors/wechat/config', { method: 'PUT', body: JSON.stringify(value) }) }

export async function unbindConnector(platform: ConnectorPlatform) {
  return knowledgeRequest<{ status: string }>(`/connectors/${encodeURIComponent(platform)}`, { method: 'DELETE' })
}


export async function discoverConversations(platform: ConnectorPlatform) {
  return knowledgeRequest<DiscoveryDTO>(`/connectors/${encodeURIComponent(platform)}/conversations/discover`)
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

export async function getConversationDetail(conversationID: string, limit = 200): Promise<ConversationDetail> {
  const conversation = await knowledgeRequest<ConversationDTO>(`/conversations/${encodeURIComponent(conversationID)}`)
  const [messageBody, attachmentBody] = await Promise.all([
    knowledgeRequest<{ items: MessageDTO[] }>(`/conversations/${encodeURIComponent(conversationID)}/messages?limit=${limit}`),
    knowledgeRequest<{ items: AttachmentDTO[] }>(`/conversations/${encodeURIComponent(conversationID)}/attachments`),
  ])
  return { conversation, messages: messageBody.items || [], attachments: attachmentBody.items || [] }
}

export async function getKnowledgeAttachmentContent(id: string, download = false) {
  const response = await fetch(knowledgeContentURL(`/attachments/${encodeURIComponent(id)}/content`), {
    headers: knowledgeHeaders(undefined, download ? 'application/octet-stream' : '*/*'),
  })
  if (!response.ok) {
    let error: any = {}
    try { error = await response.json() } catch { /* empty error body */ }
    throw new ApiError(error.message || '附件内容暂不可用', error.code || 'attachment_not_ready', response.status, Boolean(error.retryable))
  }
  return response.blob()
}
