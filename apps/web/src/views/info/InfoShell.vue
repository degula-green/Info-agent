<template>
  <div class="info-shell" :class="{ 'info-shell--sidebar-collapsed': sidebarCollapsed }">
    <InfoSidebar
      :active="activeKey"
      :nickname="sidebarNickname"
      :avatar="sidebarAvatar"
      :avatar-url="sidebarAvatarUrl"
      :history-items="sidebarHistoryItems"
      :active-history-id="sidebarActiveHistoryID"
      @navigate="navigate"
      @history-select="selectSidebarHistory"
      @history-rename="renameSidebarHistory"
      @history-delete="deleteSidebarHistory"
      @collapsed-change="sidebarCollapsed = $event"
      @menu-action="handleUserMenuAction"
    />
    <main class="info-shell__main">
      <header v-if="route.name !== 'chat'" class="info-shell__header">
        <div class="info-shell__crumb"><span class="info-shell__product">Info Agent</span><span class="info-shell__slash">/</span><strong>{{ pageTitle }}</strong></div>
        <button type="button" class="info-shell__search" @click="openSearch"><t-icon name="search" /><span>搜索消息、文件、群聊或问答</span><kbd>Ctrl K</kbd></button>
      </header>
      <div class="info-shell__content"><RouterView /></div>
    </main>
    <TodoFloatingPanel />
    <InfoCommandPalette :visible="paletteVisible" :query="paletteQuery" :results="paletteResults" :loading="paletteLoading" :empty-hint="paletteEmptyHint" :recent-searches="store.recentSearches" @update:visible="paletteVisible = $event" @search="runPaletteSearch" @select="selectPaletteResult" />
    <InfoSearchPreviewDialog
      v-model:visible="previewVisible"
      :result="previewResult"
      :file="previewFile"
      :downloading="previewDownloading"
      @open-source="openPreviewSource"
      @download="downloadPreviewFile"
    />
    <t-dialog v-model:visible="toastDialogVisible" header="提示" :footer="false" width="360px"><p class="info-toast-dialog">{{ toastText }}</p></t-dialog>
    <t-dialog
      v-model:visible="renameDialogVisible"
      header="重命名会话"
      width="420px"
      :confirm-btn="{ content: '保存', loading: qaDialogSubmitting, disabled: !renameTitle.trim() }"
      :cancel-btn="{ content: '取消', disabled: qaDialogSubmitting }"
      :close-btn="!qaDialogSubmitting"
      @confirm="confirmRenameQaSession"
    >
      <div class="qa-dialog">
        <p>修改后的名称会同步显示在历史会话列表中。</p>
        <t-input
          v-model="renameTitle"
          autofocus
          clearable
          maxlength="60"
          placeholder="请输入会话名称"
          @keydown.enter.prevent="confirmRenameQaSession"
        />
      </div>
    </t-dialog>
    <t-dialog
      v-model:visible="deleteDialogVisible"
      header="删除历史会话"
      width="420px"
      :confirm-btn="{ content: '删除', theme: 'danger', loading: qaDialogSubmitting }"
      :cancel-btn="{ content: '取消', disabled: qaDialogSubmitting }"
      :close-btn="!qaDialogSubmitting"
      @confirm="confirmDeleteQaSession"
    >
      <div class="qa-dialog qa-dialog--danger">
        <p>删除后将无法恢复这条问答历史，确认继续吗？</p>
      </div>
    </t-dialog>
    <t-dialog v-model:visible="protocolDialogVisible" :header="protocolTitle" :footer="false" width="520px">
      <div class="protocol-dialog">
        <p>{{ protocolText }}</p>
        <p class="protocol-dialog__updated">最后更新：2026 年 8 月</p>
      </div>
    </t-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import InfoSidebar from '@/components/InfoSidebar.vue'
import TodoFloatingPanel from '@/components/TodoFloatingPanel.vue'
import InfoCommandPalette from '@/components/InfoCommandPalette.vue'
import InfoSearchPreviewDialog from '@/components/InfoSearchPreviewDialog.vue'
import { type SearchResult } from '@/mock'
import { useInfoMockStore } from '@/stores/infoMock'
import { useAuthStore } from '@/stores/auth'
import { normalizeSourceKey, useInfoKnowledgeStore } from '@/stores/infoKnowledge'
import { getProfile } from '@/mock-api/info-profile'
import { downloadAvatar, getCurrentUser } from '@/api/core-auth'
import { searchGlobal } from '@/api/rag'
import { resolveGlobalSearchScope, searchEmptyHint } from '@/utils/info-search-scope'
import { isAbortError, mapRagSearchItems } from '@/utils/info-search-result'
import { useSearchResultNavigation } from '@/composables/useSearchResultNavigation'
import { listQaConversations, type QaConversation } from '@/mock-api/qa-history'
import { renameQaConversation, deleteQaConversation } from '@/mock-api/qa-history'
import {
  deleteAgentConversation,
  listAgentConversations,
  renameAgentConversation,
  type AgentConversationSummary,
} from '@/api/info-agent'

const store = useInfoMockStore(); const auth = useAuthStore(); const knowledgeStore = useInfoKnowledgeStore(); const router = useRouter(); const route = useRoute()
const { previewVisible, previewResult, previewFile, previewDownloading, openSearchResult, openPreviewSource, downloadPreviewFile } = useSearchResultNavigation()
// Keep the mock profile out of the initial render; the profile API is authoritative.
const sidebarNickname = ref('')
const sidebarAvatar = ref('')
const sidebarAvatarUrl = ref<string | null>(null)
const qaConversations = ref<QaConversation[]>([])
const agentConversations = ref<AgentConversationSummary[]>([])
const renameDialogVisible = ref(false)
const renameSessionId = ref('')
const renameTitle = ref('')
const deleteDialogVisible = ref(false)
const deleteSessionId = ref('')
const qaDialogSubmitting = ref(false)
const historyDialogMode = ref<'agent' | 'qa'>('qa')
// The Agent conversation is the application-level history. Keep it visible
// while navigating knowledge, contacts and other pages; only the legacy
// /rag-chat page still points at the old QA history source.
const sidebarHistoryMode = computed<'agent' | 'qa'>(() => route.name === 'ragChat' ? 'qa' : 'agent')
const sidebarHistoryItems = computed(() => (
  sidebarHistoryMode.value === 'agent'
    ? agentConversations.value.map((item) => ({
        id: item.conversation_id,
        title: item.title || '新的对话',
        subtitle: `${item.message_count || 0} 条消息`,
      }))
    : qaConversations.value.map((item) => ({
        id: String(item.id),
        title: item.title,
        subtitle: `${item.message_count} 条问答`,
      }))
))
const sidebarActiveHistoryID = computed(() => (
  sidebarHistoryMode.value === 'agent'
    ? String(route.query.conversation || '')
    : String(route.query.session || '')
))
async function refreshQaConversations() {
  try { qaConversations.value = (await listQaConversations()).items || [] } catch {
    // Keep the last successful list during transient route/API failures.
    // Clearing it here makes persisted history look deleted until the next refresh.
  }
}
async function refreshAgentConversations() {
  try { agentConversations.value = (await listAgentConversations(1, 100)).items || [] } catch {
    // Keep the last successful list during transient route/API failures.
  }
}
async function refreshSidebarHistory() {
  if (sidebarHistoryMode.value === 'agent') await refreshAgentConversations()
  else await refreshQaConversations()
}
onMounted(async () => {
  try {
    const user = await getCurrentUser()
    sidebarNickname.value = user.nickname || user.email.split('@')[0]
    sidebarAvatarUrl.value = user.avatar_url ? await downloadAvatar().catch(() => null) : null
    sidebarAvatar.value = sidebarNickname.value.slice(0, 1) || sidebarAvatar.value
    store.updateProfile({ nickname: sidebarNickname.value, email: user.email, avatar: sidebarAvatar.value })
  } catch {
    try {
      const profile = await getProfile()
      sidebarNickname.value = profile.nickname || profile.username || store.profile.nickname
      sidebarAvatarUrl.value = profile.avatar_url || null
      sidebarAvatar.value = sidebarNickname.value.slice(0, 1) || store.profile.avatar
      store.updateProfile({ nickname: sidebarNickname.value, email: profile.email, avatar: sidebarAvatar.value })
    } catch {
      sidebarNickname.value = store.profile.nickname
      sidebarAvatar.value = store.profile.avatar
    }
  }
  await refreshSidebarHistory()
  await knowledgeStore.ensureSources()
})
function handleAvatarUpdated(event: Event) { sidebarAvatarUrl.value = (event as CustomEvent<string | null>).detail || null }
onMounted(() => window.addEventListener('profile-avatar-updated', handleAvatarUpdated))
function handleAgentConversationUpdated() {
  if (sidebarHistoryMode.value === 'agent') void refreshAgentConversations()
}
onMounted(() => window.addEventListener('agent-conversation-updated', handleAgentConversationUpdated))
onBeforeUnmount(() => {
  window.removeEventListener('profile-avatar-updated', handleAvatarUpdated)
  window.removeEventListener('agent-conversation-updated', handleAgentConversationUpdated)
})
function handleGlobalSearchShortcut(event: KeyboardEvent) {
  if (event.defaultPrevented || event.repeat || event.isComposing) return
  if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return
  event.preventDefault()
  if (!paletteVisible.value) openSearch()
}
onMounted(() => window.addEventListener('keydown', handleGlobalSearchShortcut, { capture: true }))
onBeforeUnmount(() => window.removeEventListener('keydown', handleGlobalSearchShortcut, { capture: true }))
watch(() => route.fullPath, () => { void refreshSidebarHistory() })
watch(() => store.qaSessions, () => { if (sidebarHistoryMode.value === 'qa') void refreshQaConversations() }, { deep: true })
const knowledgePollTimer = ref<number | null>(null)
onMounted(() => {
  // Keep the knowledge directory stable while navigating. A forced periodic
  // refresh can replace a complete snapshot with a partial page and make
  // conversations appear/disappear. Explicit refresh actions still call
  // refreshSources(true).
  knowledgePollTimer.value = window.setInterval(() => {
    // Keep a detail route stable. InfoConversationPage refreshes the current
    // conversation directly, while replacing the whole directory snapshot
    // can make an otherwise valid conversation disappear temporarily.
    if (route.name === 'conversation') return
    void knowledgeStore.ensureSources()
  }, 30000)
})
onBeforeUnmount(() => {
  if (knowledgePollTimer.value != null) window.clearInterval(knowledgePollTimer.value)
  if (paletteSearchTimer) clearTimeout(paletteSearchTimer)
  paletteAbort?.abort()
})
const sidebarCollapsed = ref(false); const paletteVisible = ref(false); const paletteQuery = ref(''); const paletteResults = ref<SearchResult[]>([]); const paletteLoading = ref(false); const paletteEmptyHint = ref(''); const toastText = ref(''); const toastDialogVisible = ref(false); const protocolDialogVisible = ref(false); const protocolType = ref<'terms' | 'privacy'>('terms'); let paletteSearchTimer: ReturnType<typeof setTimeout> | undefined; let paletteSearchSeq = 0
const activeKey = computed(() => {
  if (paletteVisible.value) return 'search'
  if (route.name === 'chat' || route.name === 'ragChat') return 'new-chat'; if (route.name === 'search') return 'search'; if (String(route.name || '').startsWith('knowledge') || route.name === 'conversation' || route.path.startsWith('/knowledge')) return 'knowledge'; if (route.name === 'organization' || route.name === 'organizationKnowledge' || route.name === 'organizationEntityReview') return 'organization'; if (route.name === 'contacts') return 'contacts'; if (route.name === 'profile') return 'profile'; return 'new-chat'
})
const pageTitle = computed(() => ({ dashboard: '概览', search: '搜索', knowledge: '知识库', knowledgeOrganizationFiles: '组织文件库', knowledgeOrganizationGroups: '组织群聊', knowledgeOrganizationPrivateShared: '组织共享私聊', knowledgePersonalPrivate: '私人私聊', knowledgePersonalFiles: '私人本地知识库', knowledgePlatform: '知识库', conversation: '会话详情', organization: '我的组织', organizationKnowledge: '知识结构', organizationEntityReview: '候选实体审核', contacts: '联系人列表', chat: 'Agent 对话', ragChat: '知识问答', profile: '个人中心' } as Record<string, string>)[String(route.name)] || (route.params.platform ? knowledgeStore.findSource(normalizeSourceKey(String(route.params.platform)) || undefined)?.kbName || store.findSource(normalizeSourceKey(String(route.params.platform)) || undefined)?.kbName || '知识库' : '概览'))

async function navigate(view: string) {
  if (view === 'search') { openSearch(); return }
  if (view === 'new-chat') {
    // Creating a conversation is deferred until the first question is sent.
    // Repeated clicks on "新对话" must not create empty history rows.
    if (route.name !== 'chat' || route.query.conversation) router.push('/chat')
    return
  }
  router.push(view === 'knowledge' ? '/knowledge' : view === 'organization' ? '/organization' : view === 'contacts' ? '/contacts' : view === 'profile' || view === 'capabilities' ? '/profile' : '/dashboard')
}
const protocolTitle = computed(() => protocolType.value === 'terms' ? '用户协议' : '隐私协议')
const protocolText = computed(() => protocolType.value === 'terms'
  ? '使用 Info Agent 即表示你同意遵守适用的法律法规，并仅在已获得授权的范围内使用信息采集、检索和问答功能。你应妥善保管账号信息，不得将平台用于未经授权的数据访问。'
  : 'Info Agent 仅处理你主动绑定并授权采集的第三方账号数据。采集内容仅供你的账号使用，平台不会将私人数据开放给其他用户；你可以随时暂停采集或解除绑定。')
function handleUserMenuAction(action: 'profile' | 'terms' | 'privacy' | 'logout') {
  if (action === 'profile') { router.push('/profile'); return }
  if (action === 'terms' || action === 'privacy') { protocolType.value = action; protocolDialogVisible.value = true; return }
  void auth.logout(); store.logout(); router.push('/login')
}
function selectSidebarHistory(id: string) {
  if (sidebarHistoryMode.value === 'agent') {
    router.push({ path: '/chat', query: { conversation: id } })
    return
  }
  router.push({ path: '/rag-chat', query: { session: id } })
}
function renameSidebarHistory(id: string) {
  historyDialogMode.value = sidebarHistoryMode.value
  const current = historyDialogMode.value === 'agent'
    ? agentConversations.value.find((item) => item.conversation_id === id)
    : qaConversations.value.find((item) => String(item.id) === id)
  if (!current) { MessagePlugin.error('未找到该历史会话'); return }
  renameSessionId.value = id
  renameTitle.value = current.title || ''
  renameDialogVisible.value = true
}
async function confirmRenameQaSession() {
  const title = renameTitle.value.trim()
  if (!title || !renameSessionId.value || qaDialogSubmitting.value) return
  qaDialogSubmitting.value = true
  try {
    if (historyDialogMode.value === 'agent') {
      const updated = await renameAgentConversation(renameSessionId.value, title)
      const current = agentConversations.value.find((item) => item.conversation_id === renameSessionId.value)
      if (current) current.title = updated.title
    } else {
      const updated = await renameQaConversation(renameSessionId.value, title)
      const current = qaConversations.value.find((item) => String(item.id) === renameSessionId.value)
      if (current) current.title = updated.title
    }
    renameDialogVisible.value = false
    MessagePlugin.success('会话名称已更新')
  } catch {
    MessagePlugin.error('重命名失败')
  } finally {
    qaDialogSubmitting.value = false
  }
}
function deleteSidebarHistory(id: string) {
  historyDialogMode.value = sidebarHistoryMode.value
  const exists = historyDialogMode.value === 'agent'
    ? agentConversations.value.some((item) => item.conversation_id === id)
    : qaConversations.value.some((item) => String(item.id) === id)
  if (!exists) { MessagePlugin.error('未找到该历史会话'); return }
  deleteSessionId.value = id
  deleteDialogVisible.value = true
}
async function confirmDeleteQaSession() {
  const id = deleteSessionId.value
  if (!id || qaDialogSubmitting.value) return
  qaDialogSubmitting.value = true
  try {
    if (historyDialogMode.value === 'agent') {
      await deleteAgentConversation(id)
      agentConversations.value = agentConversations.value.filter((item) => item.conversation_id !== id)
    } else {
      await deleteQaConversation(id)
      qaConversations.value = qaConversations.value.filter((item) => String(item.id) !== id)
    }
    deleteDialogVisible.value = false
    if (
      historyDialogMode.value === 'agent'
        ? String(route.query.conversation || '') === id
        : String(route.query.session || '') === id
    ) {
      await router.push(historyDialogMode.value === 'agent' ? '/chat' : '/rag-chat')
    }
    MessagePlugin.success('历史会话已删除')
  } catch {
    MessagePlugin.error('删除历史失败')
  } finally {
    qaDialogSubmitting.value = false
  }
}
let paletteAbort: AbortController | null = null
function openSearch() { paletteQuery.value = ''; paletteResults.value = []; paletteEmptyHint.value = ''; paletteVisible.value = true }
function runPaletteSearch(query: string, committed = false) {
  paletteQuery.value = query
  if (paletteSearchTimer) clearTimeout(paletteSearchTimer)
  const normalized = query.trim()
  if (normalized.length < 2) {
    paletteAbort?.abort()
    paletteResults.value = []
    paletteEmptyHint.value = ''
    paletteLoading.value = false
    return
  }
  const seq = ++paletteSearchSeq
  paletteLoading.value = true
  paletteSearchTimer = setTimeout(async () => {
    paletteAbort?.abort()
    const controller = new AbortController()
    paletteAbort = controller
    try {
      const scope = await resolveGlobalSearchScope()
      if (seq !== paletteSearchSeq) return
      const response = await searchGlobal({
        query: normalized,
        organizationId: scope.organizationId,
        knowledgeBaseIds: scope.knowledgeBaseIds,
        topK: 12,
        signal: controller.signal,
      })
      if (seq !== paletteSearchSeq) return
      paletteResults.value = mapRagSearchItems(response.items)
      paletteEmptyHint.value = paletteResults.value.length ? '' : searchEmptyHint(response.diagnostics)
      paletteLoading.value = false
      if (committed) store.addRecentSearch(normalized)
    } catch (error) {
      if (isAbortError(error) || seq !== paletteSearchSeq) return
      paletteResults.value = []
      paletteEmptyHint.value = ''
      paletteLoading.value = false
      MessagePlugin.error((error as Error)?.message || '搜索服务暂不可用')
    }
  }, 180)
}
async function selectPaletteResult(result: SearchResult) {
  paletteVisible.value = false
  await openSearchResult(result)
}
</script>

<style lang="less" scoped>
.info-shell { display: flex; width: 100%; height: 100vh; min-width: 680px; overflow: hidden; background: var(--td-bg-color-container); color: var(--td-text-color-primary); }
.info-shell--sidebar-collapsed { background: #f3f4f5; }
.info-shell__main { display: flex; flex: 1; min-width: 0; flex-direction: column; }
.info-shell--sidebar-collapsed .info-shell__main { margin: 20px 20px 20px 0; overflow: hidden; border-radius: 22px; background: var(--td-bg-color-container); }
.info-shell__header { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 62px; padding: 0 28px; border-bottom: 1px solid var(--td-component-stroke); background: var(--td-bg-color-container); }
.info-shell__crumb { display: flex; align-items: center; gap: 9px; min-width: 0; font-size: 14px; }.info-shell__product { color: var(--td-brand-color); font-weight: 700; }.info-shell__slash { color: var(--td-text-color-placeholder); }.info-shell__crumb strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.info-shell__search { display: flex; align-items: center; gap: 9px; width: min(360px, 42vw); padding: 8px 10px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-placeholder); background: var(--td-bg-color-page); text-align: left; font-size: 12px; cursor: pointer; }.info-shell__search:hover { border-color: var(--td-brand-color); color: var(--td-text-color-secondary); }.info-shell__search span { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }.info-shell__search kbd { padding: 2px 5px; border: 1px solid var(--td-component-stroke); border-radius: 3px; font-size: 10px; }
.info-shell__content { flex: 1; min-height: 0; overflow: auto; }.info-toast-dialog { margin: 0; color: var(--td-text-color-secondary); }.qa-dialog p { margin: 0 0 16px; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }.qa-dialog--danger p { margin-bottom: 0; color: var(--td-text-color-primary); }.protocol-dialog { color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.8; }.protocol-dialog p { margin: 0; }.protocol-dialog__updated { margin-top: 14px !important; color: var(--td-text-color-placeholder); font-size: 12px; }
@media (max-width: 760px) { .info-shell { min-width: 0; }.info-shell__header { padding: 0 16px; }.info-shell__search { width: 40px; padding: 8px; }.info-shell__search span, .info-shell__search kbd { display: none; }.info-shell__search svg { margin: auto; } }
</style>
