export type SourceKey = 'feishu' | 'wecom' | 'wechat'
export type CollectionStatus = 'not_started' | 'collecting' | 'paused' | 'detached' | 'missing' | 'error'

export interface InfoAttachment { id: string; name: string; type: 'file' | 'image' }

export interface InfoCollector {
  id: string
  collectorUserId: string
  role: 'primary' | 'supplemental' | string
  status: 'active' | 'unavailable' | 'removed' | string
  lastCursor?: string
  lastSuccessAt?: string | null
  lastAttemptAt?: string | null
  nextPollAt?: string | null
  consecutiveFailures?: number
  lastError?: string | null
  agentOnline?: boolean
  lastHeartbeatAt?: string | null
}

export interface InfoMessage {
  id: string; sender: string; content: string; time: string; timestamp: string; collectedAt?: string; collectionTimestamp?: string
  attachments?: InfoAttachment[]; vectorStatus?: string; sourceMessageId?: string; senderAvatarUrl?: string
  isDeleted?: boolean; isUpdated?: boolean; messageType?: string; sourceMessageType?: string; metadata?: Record<string, unknown>
}

export interface InfoFile {
  id: string; name: string; type: string; mimeType?: string; size: string; time: string
    uploadedAt: string; uploader: string; content: string; timestamp?: string; sentAt?: string; collectedAt?: string; collectionTimestamp?: string; documentId?: number | null; documentStatus?: string | null
  parseStatus?: string; previewCapability?: string; contentAccessRequired?: boolean; isDeleted?: boolean; fileSizeBytes?: number | null
}

export interface InfoChat {
  id: string; name: string; source: SourceKey; members: number; collecting: boolean
  collectionStatus: CollectionStatus; historyStart: string; interval: string
  lastSync: string; recentMessageTime: string; messages: InfoMessage[]; files: InfoFile[]
  externalId?: string; lastSeenAt?: string | null; messageCount?: number; attachmentCount?: number; selected?: boolean
  historyStartAt?: string | null; lastStoppedAt?: string | null; remoteExists?: boolean
  isDirect?: boolean
  collectors?: InfoCollector[]
}

export interface InfoAvailableSession {
  id: string; name: string; members: number; isDirect?: boolean; externalId?: string; lastSeenAt?: string | null; messageCount?: number; attachmentCount?: number
  attachedConversationId?: string; currentUserCollector?: boolean
}

export interface InfoSource {
  key: SourceKey; name: string; kbName: string; description: string
  account: string; bound: boolean; chats: InfoChat[]; availableSessions: InfoAvailableSession[]
  selectedConversationCount?: number; lastSyncAt?: string | null; enabled?: boolean; available?: boolean; historyStartAt?: string | null; lastError?: string | null; status?: 'unbound' | 'active' | 'paused' | 'error' | 'offline' | 'expired' | 'revoked' | 'reauthorization_required'
  discoveryLoading?: boolean; discoveryLoaded?: boolean; discoveryError?: string | null
  agentOnline?: boolean; lastHeartbeatAt?: string | null
}

export interface QASession {
  id: string; question: string; answer: string; summary: string
  source: '全部数据' | '飞书' | '企业微信' | '个人微信'; time: string
}

export interface SearchResult {
  id: string; kind: 'chat' | 'message' | 'file' | 'qa'; title: string; subtitle: string
  source: string; platform: SourceKey | 'all' | 'qa'; chatId?: string; recordId?: string
  content?: string; context?: InfoMessage[]; sender?: string; uploader?: string
  time?: string; score?: number; question?: string; answer?: string; citations?: Array<Record<string, unknown>>; conversationId?: string; excerpt?: string; contentAccessRequired?: boolean
}

export interface InfoProfile { nickname: string; email: string; avatar: string }
export interface InfoMockState { profile: InfoProfile; sources: InfoSource[]; qaSessions: QASession[]; recentSearches: string[] }

export const sourceColor: Record<SourceKey, string> = { feishu: '#3370ff', wecom: '#10a37f', wechat: '#07c160' }
export const sourceNameMap: Record<SourceKey, string> = { feishu: '飞书', wecom: '企业微信', wechat: '个人微信' }
export function sourceName(key: SourceKey) { return sourceNameMap[key] }

const productMessages: InfoMessage[] = [
  { id: 'm1', sender: '周宁', content: '下周一发布 2.4 版本，发布说明请在周五前确认。', time: '今天 09:42', timestamp: '2026-08-30T09:42:00+08:00' },
  { id: 'm2', sender: '林默', content: '收到，我会整理本周的用户反馈和风险项。', time: '今天 09:48', timestamp: '2026-08-30T09:48:00+08:00' },
  { id: 'm3', sender: '周宁', content: '附件里是新版需求文档，重点看第三章。', time: '昨天 18:20', timestamp: '2026-08-29T18:20:00+08:00', attachments: [{ id: 'f1', name: '产品需求文档-v2.4.pdf', type: 'file' }] },
]

function chat(overrides: Partial<InfoChat> & Pick<InfoChat, 'id' | 'name' | 'source'>): InfoChat {
  const status = overrides.collectionStatus || (overrides.collecting ? 'collecting' : 'paused')
  return { members: 8, collecting: status === 'collecting', collectionStatus: status, historyStart: status === 'not_started' ? '' : '2026-08-23', interval: '每 30 分钟', lastSync: status === 'collecting' ? '8 分钟前' : status === 'paused' ? '已暂停' : '尚未同步', recentMessageTime: status === 'not_started' ? '尚未同步' : '今天 09:48', messages: [], files: [], ...overrides }
}

export function createInitialMockState(): InfoMockState {
  return {
    profile: { nickname: '林默', email: 'linmo@example.com', avatar: '林' },
    sources: [
      { key: 'feishu', name: '飞书', kbName: '飞书知识库', description: '飞书群聊、私聊消息与文件', account: 'linmo@company.cn', bound: true, availableSessions: [
        { id: 'feishu-design', name: '设计评审群', members: 11 },
        { id: 'feishu-release', name: '版本发布协同群', members: 16 },
        { id: 'feishu-dm-wuyan', name: '林默 ↔ 吴言', members: 2, isDirect: true },
      ], chats: [
        chat({ id: 'feishu-product', name: '产品讨论组', source: 'feishu', members: 18, collecting: true, collectionStatus: 'collecting', messages: productMessages, files: [{ id: 'f1', name: '产品需求文档-v2.4.pdf', type: 'PDF', size: '2.4 MB', time: '昨天 18:20', uploadedAt: '2026-08-29 18:20', uploader: '周宁', content: '版本 2.4 需求说明\n\n一、发布计划\n下周一发布，周五完成发布说明确认。\n\n二、重点变更\n优化消息检索与来源定位。' }] }),
        chat({ id: 'feishu-admin', name: '行政协作群', source: 'feishu', members: 9, collecting: true, collectionStatus: 'collecting', historyStart: '2026-08-16', lastSync: '22 分钟前', recentMessageTime: '昨天 14:06', messages: [{ id: 'm4', sender: '王璐', content: '会议室 A 已预订，访客信息请在今天下班前提交。', time: '昨天 14:06', timestamp: '2026-08-29T14:06:00+08:00' }] }),
        chat({ id: 'feishu-dm', name: '林默 ↔ 陈曦', source: 'feishu', members: 2, isDirect: true, collecting: false, collectionStatus: 'paused', historyStart: '2026-08-20', lastSync: '已暂停', recentMessageTime: '8 月 27 日 16:10', messages: [{ id: 'm7', sender: '陈曦', content: '数据库迁移方案我看过了，建议把回滚步骤再补充一下。', time: '8 月 27 日 16:10', timestamp: '2026-08-27T16:10:00+08:00' }] }),
      ] },
      { key: 'wecom', name: '企业微信', kbName: '企业微信知识库', description: '企业微信知识库（当前不可采集消息）', account: '', bound: false, available: false, availableSessions: [], chats: [] },
      { key: 'wechat', name: '个人微信', kbName: '个人微信知识库', description: '个人微信群聊、私聊消息与文件', account: '', bound: false, availableSessions: [
        { id: 'wechat-weekend', name: '周末徒步群', members: 7 },
        { id: 'wechat-dm-yangfan', name: '林默 ↔ 杨帆', members: 2, isDirect: true },
      ], chats: [
        chat({ id: 'wechat-family', name: '家人群', source: 'wechat', members: 6, collecting: false, collectionStatus: 'not_started', lastSync: '尚未同步', recentMessageTime: '尚未同步', messages: [{ id: 'm6', sender: '妈妈', content: '周末回来吃饭吗？', time: '周日 11:32', timestamp: '2026-08-23T11:32:00+08:00' }] }),
        chat({ id: 'wechat-notes', name: '读书交流群', source: 'wechat', members: 31, collecting: false, collectionStatus: 'not_started', lastSync: '尚未同步', recentMessageTime: '尚未同步', messages: [] }),
      ] },
    ],
    qaSessions: [
      { id: 'qa-1', question: '本周有哪些需要跟进的事项？', answer: '本周需要跟进发布说明确认、用户反馈整理和客户验收时间调整。', summary: '发布计划、用户反馈与客户验收安排', source: '全部数据', time: '今天 11:20' },
      { id: 'qa-2', question: '数据库迁移方案的风险是什么？', answer: '当前上下文里提到的风险主要是回滚步骤和压测结果仍需补充。', summary: '回滚步骤与压测结果需要补齐', source: '个人微信', time: '昨天 18:05' },
    ],
    recentSearches: ['数据库迁移方案', '版本发布', '验收时间'],
  }
}

export function cloneMockState() { return structuredClone(createInitialMockState()) }
export type Attachment = InfoAttachment
export type Message = InfoMessage
export type Conversation = InfoChat
export type Source = InfoSource
export type Profile = InfoProfile
export type QaSession = QASession
export type DemoState = InfoMockState
export const sourceNameRecord = sourceNameMap
export const cloneDemoState = cloneMockState
export const sources = createInitialMockState().sources

export function searchMock(query: string, platform: SourceKey | 'all' = 'all', sourceList: InfoSource[] = sources, qaList: QASession[] = createInitialMockState().qaSessions): SearchResult[] {
  const q = query.trim().toLowerCase(); if (!q) return []
  const results: SearchResult[] = []; const visibleSources = platform === 'all' ? sourceList : sourceList.filter((item) => item.key === platform)
  for (const source of visibleSources) for (const currentChat of source.chats) {
    if (`${currentChat.name} ${source.name}`.toLowerCase().includes(q)) results.push({ id: `chat-${currentChat.id}`, kind: 'chat', title: currentChat.name, subtitle: `${source.name} · ${currentChat.members} 人 · 最近消息 ${currentChat.recentMessageTime}`, source: source.name, platform: source.key, chatId: currentChat.id, score: 0.96 })
    for (const message of currentChat.messages) if (`${message.sender} ${message.content}`.toLowerCase().includes(q)) results.push({ id: `message-${message.id}`, kind: 'message', title: message.content, subtitle: `${source.name} · ${currentChat.name} · ${message.sender} · ${message.time}`, source: source.name, platform: source.key, chatId: currentChat.id, recordId: message.id, content: message.content, sender: message.sender, time: message.time, context: currentChat.messages, score: 0.91 })
    for (const file of currentChat.files) if (`${file.name} ${file.content} ${file.uploader}`.toLowerCase().includes(q)) results.push({ id: `file-${file.id}`, kind: 'file', title: file.name, subtitle: `${source.name} · ${currentChat.name} · ${file.uploader} · ${file.uploadedAt}`, source: source.name, platform: source.key, chatId: currentChat.id, recordId: file.id, content: file.content, uploader: file.uploader, time: file.uploadedAt, contentAccessRequired: file.contentAccessRequired, score: 0.89 })
  }
  for (const qa of qaList) if ((platform === 'all' || qa.source === sourceName(platform)) && `${qa.question} ${qa.answer} ${qa.summary}`.toLowerCase().includes(q)) results.push({ id: qa.id, kind: 'qa', title: qa.question, subtitle: `${qa.source} · ${qa.time} · ${qa.summary}`, source: qa.source, platform: 'qa', recordId: qa.id, content: qa.answer, time: qa.time, score: 0.86 })
  return results
}
