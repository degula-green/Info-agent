import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useInfoMockStore } from './infoMock'
import type { CollectionStatus, InfoChat, InfoSource, SourceKey } from '@/mock'

export function normalizeSourceKey(value?: string | null): SourceKey | null { const text = String(value || '').toLowerCase().replace('-', '_'); return text === 'personal_wechat' || text === 'personalwechat' ? 'wechat' : (['feishu', 'wecom', 'wechat'].includes(text) ? text as SourceKey : null) }

export const useInfoKnowledgeStore = defineStore('infoKnowledge', () => {
  const mock = useInfoMockStore()
  const loading = ref(false)
  let pendingLoad: Promise<InfoSource[]> | null = null
  const sources = computed(() => mock.sources)
  const allChats = computed(() => mock.allChats)
  const loadedAt = ref(Date.now())
  const loadError = ref<string | null>(null)
  function findSource(key?: SourceKey | string | null) { return mock.findSource(normalizeSourceKey(key)) }
  function findConversation(platform: SourceKey | string, id: string | number) { return findSource(normalizeSourceKey(platform))?.chats.find((chat) => String(chat.id) === String(id) || String(chat.externalId || '') === String(id)) }
  async function ensureSources(_force = false) {
    if (pendingLoad) return pendingLoad
    loading.value = true
    pendingLoad = new Promise<InfoSource[]>((resolve) => {
      window.setTimeout(() => {
        loadedAt.value = Date.now()
        loading.value = false
        pendingLoad = null
        resolve(sources.value)
      }, _force ? 220 : 120)
    })
    return pendingLoad
  }
  async function refreshSources(force = true) { return ensureSources(force) }
  async function refreshAvailableSessions(_platform: SourceKey) { return ensureSources(true) }
  async function accessSession(platform: SourceKey, id: string, historyStart?: string | null) { return mock.accessSession(platform, id, historyStart) }
  async function pauseConversation(platform: SourceKey, id: string) { const chat = findConversation(platform, id); if (chat) mock.setCollection(chat, 'paused'); return chat }
  async function loadConversation(platform: SourceKey, id: string, _force = false) { return findConversation(platform, id) }
  function updateMessage(chatId: string, messageId: string, content: string) { mock.updateMessage(chatId, messageId, content) }
  function updateFile(chatId: string, fileId: string, content: string) { mock.updateFile(chatId, fileId, content) }
  return { sources, allChats, loading, loadedAt, loadError, findSource, findConversation, ensureSources, refreshSources, refreshAvailableSessions, accessSession, pauseConversation, loadConversation, updateMessage, updateFile }
})
