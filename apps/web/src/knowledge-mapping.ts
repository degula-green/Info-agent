export type CollectionStatus = 'not_started' | 'collecting' | 'paused' | 'detached' | 'missing' | 'error'
export type DiscoveryAction = 'attach' | 'join' | 'attached'
export type KnowledgeDisplayStatus = 'collected' | 'parsed' | 'searchable' | 'failed'

export type LoadedSearchResult = {
  id: string
  kind: 'chat' | 'message' | 'file'
  title: string
  subtitle: string
  source: string
  platform: string
  chatId?: string
  conversationId?: string
  recordId?: string
  content?: string
  sender?: string
  uploader?: string
  time?: string
  score?: number
  context?: unknown[]
  contentAccessRequired?: boolean
}

export function mapCollectionStatus(status: string): CollectionStatus {
  if (status === 'active') return 'collecting'
  if (status === 'paused') return 'paused'
  if (status === 'detached') return 'detached'
  if (status === 'error') return 'error'
  return 'not_started'
}

export function mapAttachmentStatus(status: string): 'completed' | 'failed' | 'processing' {
  if (status === 'ready') return 'completed'
  if (status === 'failed') return 'failed'
  return 'processing'
}

export function mapKnowledgeDisplayStatus(input: { contentStatus?: string | null; ragStatus?: string | null; searchable?: boolean }): KnowledgeDisplayStatus {
  if (input.contentStatus === 'failed' || input.ragStatus === 'failed') return 'failed'
  if (input.ragStatus === 'succeeded' && input.searchable === true) return 'searchable'
  if (input.contentStatus === 'ready') return 'parsed'
  return 'collected'
}

export function knowledgeDisplayLabel(status: KnowledgeDisplayStatus): string {
  return { collected: '已采集', parsed: '解析完成', searchable: '可检索', failed: '处理失败' }[status]
}

export function isPrivateConversation(conversationType: string): boolean {
  return conversationType === 'private'
}

export function discoveryAction(value: { attachedConversationId?: string; currentUserCollector?: boolean }): DiscoveryAction {
  if (value.currentUserCollector) return 'attached'
  return value.attachedConversationId ? 'join' : 'attach'
}

export function isHistoryStartAllowed(value: string | Date | null | undefined, now = new Date(), maxDays = 7): boolean {
  if (!value) return true
  const candidate = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(candidate.getTime())) return false
  const current = now.getTime()
  const oldest = current - maxDays * 24 * 60 * 60 * 1000
  return candidate.getTime() >= oldest && candidate.getTime() <= current
}

type SearchableMessage = {
  id: string
  sender: string
  content: string
  time: string
  attachments?: unknown[]
}

type SearchableFile = {
  id: string
  name: string
  content: string
  uploader?: string
  uploadedAt?: string
  time?: string
  contentAccessRequired?: boolean
}

export type OAuthCallbackNotice = { kind: 'success' | 'error'; message: string }

function queryValue(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] || '')
  return typeof value === 'string' ? value : ''
}

export function oauthCallbackNotice(query: Record<string, unknown>): OAuthCallbackNotice | null {
  if (queryValue(query.connector) !== 'feishu') return null
  const error = queryValue(query.error)
  if (error) {
    const messages: Record<string, string> = {
      invalid_oauth_state: '飞书授权已失效，请重新发起授权',
      oauth_denied: '已取消飞书授权',
      connector_already_bound: '该飞书账号已绑定其他用户',
    }
    return { kind: 'error', message: messages[error] || '飞书授权失败，请重试' }
  }
  if (queryValue(query.status) === 'active') return { kind: 'success', message: '飞书已绑定' }
  return null
}

type SearchableChat = {
  id: string
  name: string
  members: number
  externalId?: string
  recentMessageTime: string
  messages: SearchableMessage[]
  files: SearchableFile[]
}

type SearchableSource = {
  key: string
  name: string
  chats: SearchableChat[]
}

export function searchLoadedSources(query: string, platform: string, sources: SearchableSource[]): LoadedSearchResult[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return []
  const visible = platform === 'all' ? sources : sources.filter((source) => source.key === platform)
  const results: LoadedSearchResult[] = []
  for (const source of visible) {
    for (const chat of source.chats) {
      const chatText = `${chat.name} ${source.name} ${chat.externalId || ''}`.toLowerCase()
      if (chatText.includes(needle)) {
        results.push({
          id: `chat-${chat.id}`,
          kind: 'chat',
          title: chat.name,
          subtitle: `${source.name} · ${chat.members} 人 · 最近消息 ${chat.recentMessageTime}`,
          source: source.name,
          platform: source.key,
          chatId: chat.id,
          conversationId: chat.id,
          score: 0.96,
        })
      }
      for (const message of chat.messages) {
        if (!`${message.sender} ${message.content} ${chat.name}`.toLowerCase().includes(needle)) continue
        results.push({
          id: `message-${message.id}`,
          kind: 'message',
          title: message.content || '（空消息）',
          subtitle: `${source.name} · ${chat.name} · ${message.sender} · ${message.time}`,
          source: source.name,
          platform: source.key,
          chatId: chat.id,
          conversationId: chat.id,
          recordId: message.id,
          content: message.content,
          sender: message.sender,
          time: message.time,
          context: chat.messages,
          score: 0.91,
        })
      }
      for (const file of chat.files) {
        if (!`${file.name} ${file.content} ${file.uploader || ''} ${chat.name}`.toLowerCase().includes(needle)) continue
        results.push({
          id: `file-${file.id}`,
          kind: 'file',
          title: file.name,
          subtitle: `${source.name} · ${chat.name} · ${file.uploader || '未知来源'} · ${file.uploadedAt || file.time || ''}`,
          source: source.name,
          platform: source.key,
          chatId: chat.id,
          conversationId: chat.id,
          recordId: file.id,
          content: file.content,
          uploader: file.uploader,
          time: file.uploadedAt || file.time,
          contentAccessRequired: file.contentAccessRequired,
          score: 0.89,
        })
      }
    }
  }
  return results.sort((left, right) => (right.score || 0) - (left.score || 0))
}
