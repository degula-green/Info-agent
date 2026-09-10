<template>
  <div class="info-shell" :class="{ 'info-shell--sidebar-collapsed': sidebarCollapsed }">
    <InfoSidebar
      :active="activeKey"
      :nickname="sidebarNickname"
      :avatar="sidebarAvatar"
      :avatar-url="sidebarAvatarUrl"
      :qa-sessions="qaConversations.map((item) => ({ id: String(item.id), question: item.title, answer: '', summary: `${item.message_count} 条问答`, source: '全部数据', time: item.last_message_at || '' }))"
      @navigate="navigate"
      @qa="openQaSession"
      @qa-rename="renameQaSession"
      @qa-delete="deleteQaSession"
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
    <InfoCommandPalette :visible="paletteVisible" :query="paletteQuery" :results="paletteResults" :loading="paletteLoading" :recent-searches="store.recentSearches" @update:visible="paletteVisible = $event" @search="runPaletteSearch" @select="selectPaletteResult" />
    <InfoResultDrawer v-model:visible="drawerVisible" :result="drawerResult" @toast="toast" />
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
import InfoCommandPalette from '@/components/InfoCommandPalette.vue'
import InfoResultDrawer from '@/components/InfoResultDrawer.vue'
import { type SearchResult } from '@/mock'
import { useInfoMockStore } from '@/stores/infoMock'
import { useAuthStore } from '@/stores/auth'
import { normalizeSourceKey, useInfoKnowledgeStore } from '@/stores/infoKnowledge'
import { getProfile } from '@/mock-api/info-profile'
import { downloadAvatar, getCurrentUser } from '@/api/core-auth'
import { listQaConversations, type QaConversation } from '@/mock-api/qa-history'
import { renameQaConversation, deleteQaConversation } from '@/mock-api/qa-history'

const store = useInfoMockStore(); const auth = useAuthStore(); const knowledgeStore = useInfoKnowledgeStore(); const router = useRouter(); const route = useRoute()
// Keep the mock profile out of the initial render; the profile API is authoritative.
const sidebarNickname = ref('')
const sidebarAvatar = ref('')
const sidebarAvatarUrl = ref<string | null>(null)
const qaConversations = ref<QaConversation[]>([])
const renameDialogVisible = ref(false)
const renameSessionId = ref('')
const renameTitle = ref('')
const deleteDialogVisible = ref(false)
const deleteSessionId = ref('')
const qaDialogSubmitting = ref(false)
async function refreshQaConversations() {
  try { qaConversations.value = (await listQaConversations()).items || [] } catch {
    // Keep the last successful list during transient route/API failures.
    // Clearing it here makes persisted history look deleted until the next refresh.
  }
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
  await refreshQaConversations()
  await knowledgeStore.ensureSources()
})
function handleAvatarUpdated(event: Event) { sidebarAvatarUrl.value = (event as CustomEvent<string | null>).detail || null }
onMounted(() => window.addEventListener('profile-avatar-updated', handleAvatarUpdated))
onBeforeUnmount(() => window.removeEventListener('profile-avatar-updated', handleAvatarUpdated))
watch(() => route.fullPath, () => { void refreshQaConversations() })
watch(() => store.qaSessions, () => { void refreshQaConversations() }, { deep: true })
const knowledgePollTimer = ref<number | null>(null)
onMounted(() => {
  // Keep the knowledge directory stable while navigating. A forced periodic
  // refresh can replace a complete snapshot with a partial page and make
  // conversations appear/disappear. Explicit refresh actions still call
  // refreshSources(true).
  knowledgePollTimer.value = window.setInterval(() => { void knowledgeStore.ensureSources() }, 30000)
})
onBeforeUnmount(() => {
  if (knowledgePollTimer.value != null) window.clearInterval(knowledgePollTimer.value)
})
const sidebarCollapsed = ref(false); const paletteVisible = ref(false); const paletteQuery = ref(''); const paletteResults = ref<SearchResult[]>([]); const paletteLoading = ref(false); const drawerVisible = ref(false); const drawerResult = ref<SearchResult | null>(null); const toastText = ref(''); const toastDialogVisible = ref(false); const protocolDialogVisible = ref(false); const protocolType = ref<'terms' | 'privacy'>('terms'); let paletteSearchTimer: ReturnType<typeof setTimeout> | undefined; let paletteSearchSeq = 0
const activeKey = computed(() => {
  if (paletteVisible.value) return 'search'
  if (route.name === 'chat') return 'new-chat'; if (route.name === 'search') return 'search'; if (route.name === 'knowledge' || route.name === 'knowledgePlatform' || route.params.platform) return 'knowledge'; if (route.name === 'organization') return 'organization'; if (route.name === 'profile') return 'profile'; return 'new-chat'
})
const pageTitle = computed(() => ({ dashboard: '概览', search: '搜索', knowledge: '知识库', organization: '我的组织', chat: '新对话', profile: '个人中心' } as Record<string, string>)[String(route.name)] || (route.params.platform ? knowledgeStore.findSource(normalizeSourceKey(String(route.params.platform)) || undefined)?.kbName || store.findSource(normalizeSourceKey(String(route.params.platform)) || undefined)?.kbName || '知识库' : '概览'))

async function navigate(view: string) {
  if (view === 'search') { openSearch(); return }
  if (view === 'new-chat') {
    // Creating a conversation is deferred until the first question is sent.
    // Repeated clicks on "新对话" must not create empty history rows.
    if (route.name !== 'chat' || route.query.session) router.push('/chat')
    return
  }
  router.push(view === 'knowledge' ? '/knowledge' : view === 'organization' ? '/organization' : view === 'profile' || view === 'capabilities' ? '/profile' : '/dashboard')
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
function openQaSession(id: string) { router.push({ path: '/chat', query: { session: id } }) }
function renameQaSession(id: string) {
  const current = qaConversations.value.find((item) => String(item.id) === id)
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
    const updated = await renameQaConversation(renameSessionId.value, title)
    const current = qaConversations.value.find((item) => String(item.id) === renameSessionId.value)
    if (current) current.title = updated.title
    renameDialogVisible.value = false
    MessagePlugin.success('会话名称已更新')
  } catch {
    MessagePlugin.error('重命名失败')
  } finally {
    qaDialogSubmitting.value = false
  }
}
function deleteQaSession(id: string) {
  if (!qaConversations.value.some((item) => String(item.id) === id)) { MessagePlugin.error('未找到该历史会话'); return }
  deleteSessionId.value = id
  deleteDialogVisible.value = true
}
async function confirmDeleteQaSession() {
  const id = deleteSessionId.value
  if (!id || qaDialogSubmitting.value) return
  qaDialogSubmitting.value = true
  try {
    await deleteQaConversation(id)
    qaConversations.value = qaConversations.value.filter((item) => String(item.id) !== id)
    deleteDialogVisible.value = false
    if (String(route.query.session || '') === id) await router.push('/chat')
    MessagePlugin.success('历史会话已删除')
  } catch {
    MessagePlugin.error('删除历史失败')
  } finally {
    qaDialogSubmitting.value = false
  }
}
function openSearch() { paletteQuery.value = ''; paletteResults.value = []; paletteVisible.value = true }
function runPaletteSearch(query: string, committed = false) {
  paletteQuery.value = query
  if (paletteSearchTimer) clearTimeout(paletteSearchTimer)
  const normalized = query.trim()
  if (normalized.length < 2) { paletteResults.value = []; paletteLoading.value = false; return }
  const seq = ++paletteSearchSeq
  paletteLoading.value = true
  paletteSearchTimer = setTimeout(async () => {
    try {
      await knowledgeStore.ensureSources()
      if (seq !== paletteSearchSeq) return
      paletteLoading.value = false
      paletteResults.value = knowledgeStore.search(normalized).slice(0, 20)
      if (committed) store.addRecentSearch(normalized)
    } catch { if (seq === paletteSearchSeq) { paletteResults.value = []; paletteLoading.value = false } }
  }, 180)
}
function selectPaletteResult(result: SearchResult) {
  paletteVisible.value = false
  if (result.kind === 'chat' && result.chatId) { router.push(`/knowledge/${result.platform}/conversations/${result.chatId}`); return }
  drawerResult.value = result; drawerVisible.value = true
}
function toast(text: string) { MessagePlugin.success(text) }
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
