import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  attachConversation,
  addConversationCollector,
  discoverConversations,
  getConnectors,
  getConversationDetail,
  listConversations,
  removeConversationCollector,
  setConversationStatus,
  type AttachmentDTO,
  type ConnectorDTO,
  type ConversationDTO,
  type DiscoveryDTO,
  type MessageDTO,
} from '@/api/info-knowledge'
import type { InfoAvailableSession, InfoChat, InfoCollector, InfoFile, InfoMessage, InfoSource, SearchResult, SourceKey } from '@/mock'
import { discoveryAction, isPrivateConversation, mapAttachmentStatus, mapCollectionStatus, searchLoadedSources } from '@/knowledge-mapping'

const sourceMeta: Record<SourceKey, Pick<InfoSource, 'name' | 'kbName' | 'description'>> = {
  feishu: { name: '飞书', kbName: '飞书知识库', description: '飞书群聊、私聊消息与文件' },
  wecom: { name: '企业微信', kbName: '企业微信知识库', description: '企业微信知识库（当前不可采集消息）' },
  wechat: { name: '个人微信', kbName: '个人微信知识库', description: '个人微信群聊、私聊消息与文件' },
}

export function normalizeSourceKey(value?: string | null): SourceKey | null {
  const text = String(value || '').toLowerCase().replace('-', '_')
  return text === 'personal_wechat' || text === 'personalwechat' ? 'wechat' : (['feishu', 'wecom', 'wechat'].includes(text) ? text as SourceKey : null)
}

function emptySource(key: SourceKey): InfoSource {
  return { key, ...sourceMeta[key], account: '', bound: false, availableSessions: [], chats: [], available: key !== 'wecom', status: 'unbound' }
}

function createSources() { return (['feishu', 'wecom', 'wechat'] as SourceKey[]).map(emptySource) }

function toDate(value?: string | null) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function displayTime(value?: string | null) {
  const date = toDate(value)
  if (!date) return '尚未同步'
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
}

function relativeTime(value?: string | null) {
  const date = toDate(value)
  if (!date) return '尚未同步'
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000))
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return displayTime(value)
}

function formatSize(bytes: number) {
  if (!bytes || bytes < 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function mapAttachment(value: AttachmentDTO, uploader = ''): InfoFile {
  const extension = value.file_name.includes('.') ? value.file_name.split('.').pop() || '' : ''
  return {
    id: value.id,
    name: value.file_name || '附件',
    type: extension || value.mime_type || 'FILE',
    size: formatSize(value.size_bytes),
    time: displayTime(value.created_at),
    uploadedAt: displayTime(value.created_at),
    uploader,
    content: '',
    documentStatus: mapAttachmentStatus(value.content_status),
    parseStatus: value.content_status,
    previewCapability: value.preview_capability,
    contentAccessRequired: value.content_access_required,
    fileSizeBytes: value.size_bytes,
  }
}

function mapMessage(value: MessageDTO, attachments: AttachmentDTO[]): InfoMessage {
  const related = attachments.filter((item) => item.message_id === value.id)
  return {
    id: value.id,
    sender: value.sender_display_name || value.sender_identity_id || '未知发送人',
    content: value.content || '',
    time: displayTime(value.sent_at),
    timestamp: value.sent_at,
    sourceMessageId: value.external_message_id,
    messageType: value.message_type,
    vectorStatus: value.vector_status,
    attachments: related.map((item) => ({ id: item.id, name: item.file_name, type: item.mime_type?.startsWith('image/') ? 'image' : 'file' })),
  }
}

function mapConversation(value: ConversationDTO, messages: MessageDTO[] = [], attachments: AttachmentDTO[] = []): InfoChat {
  const mappedMessages = messages.map((item) => mapMessage(item, attachments))
  const senderByAttachment = new Map(attachments.map((item) => [item.id, mappedMessages.find((message) => message.id === item.message_id)?.sender || '']))
  const mappedFiles = attachments.map((item) => mapAttachment(item, senderByAttachment.get(item.id) || ''))
  const status = mapCollectionStatus(value.status)
  return {
    id: value.id,
    externalId: value.external_conversation_id,
    name: value.name || value.external_conversation_id,
    source: normalizeSourceKey(value.platform) || 'wechat',
    members: value.memberships?.length || (isPrivateConversation(value.conversation_type) ? 2 : 0),
    isDirect: isPrivateConversation(value.conversation_type),
    collecting: status === 'collecting',
    collectionStatus: status,
    historyStart: value.effective_start_at || value.requested_start_at ? displayTime(value.effective_start_at || value.requested_start_at) : '',
    historyStartAt: value.effective_start_at || value.requested_start_at || null,
    interval: value.platform === 'wechat' ? '每 1 小时' : '每 30 分钟',
    lastSync: relativeTime(value.last_synced_at),
    recentMessageTime: mappedMessages.length ? mappedMessages[mappedMessages.length - 1].time : '尚未同步',
    messages: mappedMessages,
    files: mappedFiles,
    collectors: (value.collectors || []).map((collector) => ({
      id: collector.id,
      collectorUserId: collector.collector_user_id,
      role: collector.collector_role,
      status: collector.status,
      lastCursor: collector.last_cursor,
      lastSuccessAt: collector.last_success_at,
      lastAttemptAt: collector.last_attempt_at,
      nextPollAt: collector.next_poll_at,
      consecutiveFailures: collector.consecutive_failures,
      lastError: collector.last_error,
      agentOnline: collector.agent_online,
      lastHeartbeatAt: collector.last_heartbeat_at,
    } satisfies InfoCollector)),
    messageCount: mappedMessages.length,
    attachmentCount: mappedFiles.length,
    lastSeenAt: value.last_synced_at,
  }
}

function mapAvailable(value: { external_id: string; name: string; conversation_type: string; member_count: number; last_seen_at?: string | null; message_count?: number; attachment_count?: number; attached_conversation_id?: string; current_user_collector?: boolean }): InfoAvailableSession {
  return { id: value.external_id, externalId: value.external_id, name: value.name || value.external_id, members: value.member_count || 0, isDirect: isPrivateConversation(value.conversation_type), lastSeenAt: value.last_seen_at, messageCount: value.message_count, attachmentCount: value.attachment_count, attachedConversationId: value.attached_conversation_id, currentUserCollector: value.current_user_collector }
}

function mapConnector(value: ConnectorDTO, conversations: ConversationDTO[] = []): InfoSource {
  const key = normalizeSourceKey(value.platform) || 'wechat'
  const source = { ...emptySource(key) }
  source.bound = Boolean(value.bound)
  source.account = value.account_name || value.display_name || ''
  source.status = value.status as InfoSource['status']
  source.lastError = value.last_error || null
  source.available = value.availability === 'available'
  source.agentOnline = value.agent_online
  source.lastHeartbeatAt = value.last_heartbeat_at
  source.chats = conversations.map((item) => mapConversation(item))
  source.selectedConversationCount = source.chats.length
  source.lastSyncAt = source.chats.map((chat) => chat.lastSeenAt).filter(Boolean).sort().pop() || null
  return source
}

export const useInfoKnowledgeStore = defineStore('infoKnowledge', () => {
  const sources = ref<InfoSource[]>(createSources())
  const allChats = computed(() => sources.value.flatMap((source) => source.chats))
  const loading = ref(false)
  const loadedAt = ref(0)
  const loadError = ref<string | null>(null)
  const discoveries = new Map<SourceKey, DiscoveryDTO>()
  let pendingLoad: Promise<InfoSource[]> | null = null

  function findSource(key?: SourceKey | string | null) { return sources.value.find((source) => source.key === normalizeSourceKey(key)) }
  function findConversation(platform: SourceKey | string, id: string | number) {
    return findSource(platform)?.chats.find((chat) => String(chat.id) === String(id) || String(chat.externalId || '') === String(id))
  }

  async function loadSources() {
    const connectors = await getConnectors()
    const next = new Map<SourceKey, InfoSource>()
    for (const connector of connectors) {
      const key = normalizeSourceKey(connector.platform)
      if (!key) continue
      let conversations: ConversationDTO[] = []
      if (connector.bound && connector.availability === 'available') conversations = await listConversations(key)
      next.set(key, mapConnector(connector, conversations))
    }
    sources.value = (['feishu', 'wecom', 'wechat'] as SourceKey[]).map((key) => next.get(key) || emptySource(key))
    loadedAt.value = Date.now()
    loadError.value = null
    return sources.value
  }

  async function ensureSources(force = false) {
    if (pendingLoad && !force) return pendingLoad
    loading.value = true
    pendingLoad = loadSources().catch((error: any) => {
      loadError.value = error?.message || '知识库服务暂不可用'
      for (const source of sources.value) {
        if (source.bound) source.lastError = loadError.value
      }
      throw error
    }).finally(() => { loading.value = false; pendingLoad = null })
    return pendingLoad
  }

  async function refreshSources(force = true) { return ensureSources(force) }

  async function refreshAvailableSessions(platform: SourceKey) {
    const source = findSource(platform)
    if (!source || !source.bound || source.available === false) return []
    try {
      const discovery = await discoverConversations(platform)
      discoveries.set(platform, discovery)
      source.availableSessions = discovery.conversations.map(mapAvailable).map((session) => {
        const loaded = source.chats.find((chat) => String(chat.externalId || '') === String(session.externalId || session.id))
        return loaded ? { ...session, attachedConversationId: loaded.id, currentUserCollector: true } : session
      })
      return source.availableSessions
    } catch (error: any) {
      loadError.value = error?.message || '会话发现失败'
      source.lastError = loadError.value
      throw error
    }
  }

  async function accessSession(platform: SourceKey, id: string, historyStart?: string | null) {
    const source = findSource(platform)
    if (!source || !source.bound) return undefined
    let discovery = discoveries.get(platform)
    if (!discovery || new Date(discovery.expires_at).getTime() <= Date.now()) {
      await refreshAvailableSessions(platform)
      discovery = discoveries.get(platform)
    }
    const candidate = discovery?.conversations.find((item) => item.external_id === String(id))
    if (!candidate || !discovery) throw new Error('会话不在当前发现结果中，请刷新列表')
    const available = source.availableSessions.find((item) => String(item.externalId || item.id) === String(id)) || mapAvailable(candidate)
    const action = discoveryAction(available)
    if (action === 'attached') return findConversation(platform, available.attachedConversationId || id)
    if (action === 'join' && available.attachedConversationId) {
      await addConversationCollector(available.attachedConversationId)
      const detail = await getConversationDetail(available.attachedConversationId)
      const chat = mapConversation(detail.conversation, detail.messages, detail.attachments)
      source.chats = [chat, ...source.chats.filter((item) => item.id !== chat.id)]
      available.currentUserCollector = true
      source.selectedConversationCount = source.chats.length
      return chat
    }
    const attached = await attachConversation({ platform, externalConversationID: candidate.external_id, conversationType: candidate.conversation_type, name: candidate.name, discoveryID: discovery.discovery_id, requestedStartAt: historyStart || null })
    const chat = mapConversation(attached)
    source.chats = [chat, ...source.chats.filter((item) => item.id !== chat.id)]
    available.attachedConversationId = chat.id
    available.currentUserCollector = true
    source.selectedConversationCount = source.chats.length
    return chat
  }

  async function pauseConversation(platform: SourceKey, id: string) {
    const chat = findConversation(platform, id)
    if (!chat) return undefined
    await setConversationStatus(chat.id, 'pause')
    chat.collectionStatus = 'paused'; chat.collecting = false; chat.lastSync = '已暂停'
    return chat
  }

  async function resumeConversation(platform: SourceKey, id: string) {
    const chat = findConversation(platform, id)
    if (!chat) return undefined
    await setConversationStatus(chat.id, 'resume')
    chat.collectionStatus = 'collecting'; chat.collecting = true; chat.lastSync = '刚刚开始采集'
    return chat
  }

  async function removeCollector(platform: SourceKey, id: string, collectorID: string) {
    const chat = findConversation(platform, id)
    if (!chat) throw new Error('会话不存在或尚未加载')
    await removeConversationCollector(chat.id, collectorID)
    chat.collectors = (chat.collectors || []).map((item) => item.id === collectorID ? { ...item, status: 'removed' } : item)
    return chat
  }

  function search(query: string, platform: SourceKey | 'all' = 'all'): SearchResult[] {
    return searchLoadedSources(query, platform, sources.value) as SearchResult[]
  }

  async function loadConversation(platform: SourceKey | string, id: string, _force = false) {
    const key = normalizeSourceKey(platform)
    if (!key) return undefined
    let chat = findConversation(key, id)
    if (!chat) {
      await ensureSources(true)
      chat = findConversation(key, id)
    }
    if (!chat) return undefined
    const detail = await getConversationDetail(chat.id)
    const mapped = mapConversation(detail.conversation, detail.messages, detail.attachments)
    const source = findSource(key)
    if (source) {
      const index = source.chats.findIndex((item) => item.id === chat?.id)
      if (index >= 0) source.chats[index] = mapped
    }
    return mapped
  }

  function updateMessage(chatId: string, messageId: string, content: string) {
    const message = allChats.value.find((chat) => String(chat.id) === String(chatId))?.messages.find((item) => String(item.id) === String(messageId))
    if (message) message.content = content
  }
  function updateFile(chatId: string, fileId: string, content: string) {
    const file = allChats.value.find((chat) => String(chat.id) === String(chatId))?.files.find((item) => String(item.id) === String(fileId))
    if (file) file.content = content
  }

  return { sources, allChats, loading, loadedAt, loadError, findSource, findConversation, ensureSources, refreshSources, refreshAvailableSessions, accessSession, pauseConversation, resumeConversation, removeCollector, loadConversation, search, updateMessage, updateFile }
})
