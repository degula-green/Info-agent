import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { cloneMockState, searchMock, type CollectionStatus, type InfoChat, type InfoFile, type InfoMockState, type InfoProfile, type InfoSource, type SourceKey } from '@/mock'

const STATE_KEY = 'info_agent_mock_state_v2'
const SESSION_KEY = 'info_agent_authenticated'

function loadState(): InfoMockState {
  try {
    const raw = localStorage.getItem(STATE_KEY)
    return raw ? JSON.parse(raw) as InfoMockState : cloneMockState()
  } catch { return cloneMockState() }
}

export const useInfoMockStore = defineStore('infoMock', () => {
  const initial = loadState()
  const isAuthenticated = ref(sessionStorage.getItem(SESSION_KEY) === 'true')
  const profile = ref<InfoProfile>(initial.profile)
  const sources = ref<InfoSource[]>(initial.sources)
  const qaSessions = ref(initial.qaSessions)
  const recentSearches = ref(initial.recentSearches)
  const allChats = computed(() => sources.value.flatMap((source) => source.chats))
  const boundSources = computed(() => sources.value.filter((source) => source.bound))
  const collectingChats = computed(() => allChats.value.filter((chat) => chat.collectionStatus === 'collecting'))
  const totalMessages = computed(() => allChats.value.reduce((sum, chat) => sum + chat.messages.length, 0))
  const totalFiles = computed(() => allChats.value.reduce((sum, chat) => sum + chat.files.length, 0))
  watch([profile, sources, qaSessions, recentSearches], () => localStorage.setItem(STATE_KEY, JSON.stringify({ profile: profile.value, sources: sources.value, qaSessions: qaSessions.value, recentSearches: recentSearches.value })), { deep: true })

  function login(email: string, nickname?: string) { isAuthenticated.value = true; sessionStorage.setItem(SESSION_KEY, 'true'); profile.value.email = email; if (nickname?.trim()) profile.value.nickname = nickname.trim(); profile.value.avatar = profile.value.nickname.slice(0, 1) || '林' }
  function logout() { isAuthenticated.value = false; sessionStorage.removeItem(SESSION_KEY) }
  function findSource(key?: string | null) { return sources.value.find((source) => source.key === key) }
  function findChat(id?: string | null) { return allChats.value.find((chat) => String(chat.id) === String(id) || String(chat.externalId || '') === String(id)) }
  function bindSource(key: SourceKey) { const source = findSource(key); if (source) { source.bound = true; source.account = key === 'wechat' ? `微信 · ${profile.value.nickname}` : profile.value.email } }
  function unbindSource(key: SourceKey) { const source = findSource(key); if (source) { source.bound = false; source.account = '' } }
  function updateProfile(next: Partial<InfoProfile>) { profile.value = { ...profile.value, ...next }; profile.value.avatar = profile.value.avatar.trim() || profile.value.nickname.slice(0, 1) || '林' }
  function setCollection(chat: InfoChat, status: CollectionStatus, historyStart?: string) { chat.collectionStatus = status; chat.collecting = status === 'collecting'; if (historyStart !== undefined) chat.historyStart = historyStart; chat.lastSync = status === 'collecting' ? '刚刚开始采集' : status === 'paused' ? '已暂停' : '尚未同步'; if (status === 'collecting') chat.recentMessageTime = chat.messages.length ? chat.messages[chat.messages.length - 1].time : '等待首条消息' }
  function accessSession(sourceKey: SourceKey, sessionId: string, historyStart?: string | null) {
    const source = findSource(sourceKey)
    if (!source) return
    const existing = source.chats.find((item) => String(item.id) === String(sessionId) || String(item.externalId || '') === String(sessionId))
    if (existing) { setCollection(existing, 'collecting', historyStart === undefined ? existing.historyStart : historyStart || ''); return existing }
    const available = source.availableSessions.find((item) => String(item.externalId || item.id) === String(sessionId))
    if (!available) return
    const chat: InfoChat = { id: available.id, externalId: available.externalId, name: available.name, source: sourceKey, members: available.members, isDirect: available.isDirect, collecting: false, collectionStatus: 'not_started', historyStart: historyStart || '', historyStartAt: historyStart || null, interval: sourceKey === 'wechat' ? '每 1 小时' : '每 30 分钟', lastSync: '尚未同步', recentMessageTime: '尚未同步', messages: [], files: [] }
    source.chats.unshift(chat); source.availableSessions = source.availableSessions.filter((item) => String(item.id) !== String(sessionId)); setCollection(chat, 'collecting', historyStart || ''); return chat
  }
  function updateMessage(chatId: string, messageId: string, content: string) { const message = findChat(chatId)?.messages.find((item) => String(item.id) === String(messageId)); if (message) message.content = content }
  function updateFile(chatId: string, fileId: string, content: string) { const file = findChat(chatId)?.files.find((item) => String(item.id) === String(fileId)); if (file) file.content = content }
  function addRecentSearch(query: string) { const value = query.trim(); if (value) recentSearches.value = [value, ...recentSearches.value.filter((item) => item !== value)].slice(0, 8) }
  function search(query: string, platform: SourceKey | 'all' = 'all') { return searchMock(query, platform, sources.value, qaSessions.value) }
  async function ask(question: string, scopeIds: string[], conversationId?: string | number) {
    await new Promise((resolve) => window.setTimeout(resolve, 520))
    const q = question.toLowerCase()
    let answer = '根据当前本地演示数据，没有找到完全匹配的记录。你可以换一种说法，或扩大知识范围后重试。'
    let citations: any[] = []
    if (/本周|跟进|事项|发布|验收/.test(q)) {
      answer = '本周有三项需要跟进：周五前确认 2.4 版本发布说明；周四完成客户验收环境回归；补充数据库迁移方案中的回滚步骤和压测结果。'
      citations = [{ source: 'feishu', conversationId: 'feishu-product', messageId: 'm1', label: '产品讨论组 · 周宁' }, { source: 'feishu', conversationId: 'feishu-admin', messageId: 'm4', label: '行政协作群 · 王璐' }]
    } else if (/数据库|迁移|回滚|压测/.test(q)) {
      answer = '数据库迁移方案目前还需要补充回滚步骤和压测结果。建议上线前安排一次全量备份校验与完整回滚演练。'
      citations = [{ source: 'feishu', conversationId: 'feishu-dm', messageId: 'm7', label: '林默 ↔ 陈曦 · 陈曦' }]
    } else if (/读书|周六|交流/.test(q)) {
      answer = '读书群这周阅读《思考，快与慢》第六章，交流安排在周六晚上，形式为线上讨论。'
      citations = [{ source: 'wechat', conversationId: 'wechat-family', messageId: 'm6', label: '家人群 · 妈妈' }]
    }
    // The route ID is created before the mock request starts. Reuse it so a
    // refresh can load the just-created conversation instead of a second,
    // unrelated ID generated inside the store.
    const id = conversationId ?? `qa-${Date.now()}`
    const existing = qaSessions.value.find((item) => String(item.id) === String(id)) as any
    const session = existing || { id, title: question.slice(0, 18), question, answer, summary: answer.slice(0, 42), source: '全部数据' as const, scope: scopeIds.length ? `${scopeIds.length} 个会话` : '全部知识库', time: '刚刚', citations }
    session.title = question.slice(0, 18)
    session.question = question
    session.answer = answer
    session.summary = answer.slice(0, 42)
    session.source = '全部数据'
    session.scope = scopeIds.length ? `${scopeIds.length} 个会话` : '全部知识库'
    session.time = '刚刚'
    session.citations = citations
    if (!existing) qaSessions.value.unshift(session)
    return session
  }
  function renameQa(id: string, title: string) { const qa = qaSessions.value.find((item) => String(item.id) === String(id)); if (qa) qa.question = title.trim() || qa.question }
  function deleteQa(id: string) { qaSessions.value = qaSessions.value.filter((item) => String(item.id) !== String(id)) }
  function resetDemo() { const state = cloneMockState(); profile.value = state.profile; sources.value = state.sources; qaSessions.value = state.qaSessions; recentSearches.value = state.recentSearches; localStorage.removeItem(STATE_KEY) }
  return { isAuthenticated, profile, sources, qaSessions, recentSearches, allChats, boundSources, collectingChats, totalMessages, totalFiles, login, logout, findSource, findChat, bindSource, unbindSource, updateProfile, setCollection, accessSession, updateMessage, updateFile, addRecentSearch, search, ask, renameQa, deleteQa, resetDemo }
})
