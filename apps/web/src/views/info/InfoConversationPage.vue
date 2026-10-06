<template>
  <InfoConversation
    v-if="chat && !loadError"
    :chat="chat"
    :shared-view="sharedView"
    :my-collector-status="myCollectorStatus"
    :target-message-id="targetMessageId"
    :target-attachment-id="targetAttachmentId"
    @back="router.push(backPath)"
    @toggle="toggleChat"
    @toast="toast"
    @error="handleError"
    @share="shareSelected"
    @load-more="loadOlder"
  />
  <div v-else-if="loading" class="conversation-missing"><t-icon name="loading" size="28px" /><h3>正在加载会话</h3><p>正在从 Knowledge 加载消息和附件。</p></div>
  <div v-else class="conversation-missing"><t-icon name="error-circle" size="28px" /><h3>{{ errorTitle }}</h3><p v-if="errorDescription">{{ errorDescription }}</p><t-button theme="primary" @click="router.push('/knowledge')">返回知识库</t-button></div>

  <t-dialog
    v-model:visible="resumeDialogVisible"
    header="设置采集起点"
    :confirm-btn="{ content: '开始采集', loading: resumeLoading }"
    cancel-btn="取消"
    :close-on-overlay-click="!resumeLoading"
    @confirm="confirmResume"
  >
    <div v-if="pendingResumeChat" class="resume-dialog">
      <p v-if="pendingResumeChat.collectionStatus === 'missing'" class="resume-dialog__warning">飞书中暂时找不到这个群聊。确认开始采集时会再次检查群聊是否存在。</p>
       <p>为「{{ pendingResumeChat.name }}」恢复采集，将从已保存的检查点继续。</p>
    </div>
  </t-dialog>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import { addConversationCollector, setConversationCollectorPaused, sharePrivateResources } from '@/api/info-knowledge'
import { getAuthSession } from '@/auth/session'
import InfoConversation from '@/components/InfoConversation.vue'
import type { InfoChat } from '@/mock'
import { normalizeSourceKey, useInfoKnowledgeStore } from '@/stores/infoKnowledge'
const route = useRoute(); const router = useRouter(); const store = useInfoKnowledgeStore()
const sourceKey = computed(() => normalizeSourceKey(String(route.params.platform)) || 'wechat')
const conversationId = computed(() => String(route.params.conversationId))
const currentUserID = computed(() => {
  const token = getAuthSession().accessToken
  if (!token) return ''
  try {
    const payload = token.split('.')[1]
    if (!payload) return ''
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const decoded = globalThis.atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '='))
    return String((JSON.parse(decoded) as { sub?: string }).sub || '')
  } catch {
    return ''
  }
})
const targetMessageId = computed(() => String(route.query.message || '').trim() || null)
const targetAttachmentId = computed(() => String(route.query.attachment || '').trim() || null)
const chat = computed(() => store.findConversation(sourceKey.value, conversationId.value))
const myCollectorStatus = computed(() => {
  const userID = currentUserID.value
  if (!userID) return ''
  return chat.value?.collectors?.find((collector) => collector.collectorUserId === userID)?.status || ''
})
const backPath = computed(() => {
  const requested = String(route.query.return || '')
  if (requested.startsWith('/') && !requested.startsWith('//')) return requested
  return chat.value?.isDirect ? '/knowledge/personal/private' : `/knowledge/${chat.value?.source || sourceKey.value}`
})
const loading = ref(true)
const loadError = ref<any>(null)
const pollTimer = ref<number | null>(null)
const resumeDialogVisible = ref(false)
const resumeLoading = ref(false)
const pendingResumeChat = ref<InfoChat | null>(null)
const conversationLoads = new Map<string, Promise<void>>()
const activeKnowledgeStatuses = new Set(['not_enqueued', 'pending', 'processing'])
const conversationHasActiveWork = computed(() => {
  const current = chat.value
  if (!current) return false
  const statuses = [
    ...current.messages.map((message) => message.vectorStatus),
    ...current.files.flatMap((file) => [file.documentStatus, file.parseStatus, file.vectorStatus]),
  ]
  return statuses.some((status) => activeKnowledgeStatuses.has(String(status || '').toLowerCase()))
})

async function toggleChat(current: any) {
  if (current.collectionStatus === 'detached') return
  if (myCollectorStatus.value === 'active') {
    await setConversationCollectorPaused(conversationId.value, true)
    await store.loadConversation(sourceKey.value, conversationId.value, true)
    toast('已停止我的采集')
    return
  }
  pendingResumeChat.value = current as InfoChat
  resumeDialogVisible.value = true
}

async function confirmResume() {
  const current = pendingResumeChat.value
  if (!current || current.collectionStatus === 'detached' || resumeLoading.value) return
  resumeLoading.value = true
  try {
    try {
      await setConversationCollectorPaused(conversationId.value, false)
    } catch (error: any) {
      if (error?.code !== 'collector_not_found') throw error
      await addConversationCollector(conversationId.value)
    }
    await store.loadConversation(sourceKey.value, conversationId.value, true)
    resumeDialogVisible.value = false
    pendingResumeChat.value = null
    toast('已恢复我的采集')
  } catch (error: any) {
    MessagePlugin.error(error?.message || '无法恢复采集')
  } finally {
    resumeLoading.value = false
  }
}
function toast(text: string) { MessagePlugin.success(text) }
function handleError(text: string) { MessagePlugin.error(text) }
async function shareSelected(payload: { conversationId: string; messageIDs: string[]; attachmentIDs: string[] }) {
  try {
    const result = await sharePrivateResources({
      requestID: `web-share-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      privateConversationID: payload.conversationId,
      messageIDs: payload.messageIDs,
      attachmentIDs: payload.attachmentIDs,
    })
    const count = Number(result.shared_message_count || 0) + Number(result.shared_attachment_count || 0)
    toast(count ? `已共享 ${count} 项到组织` : '已提交共享到组织')
  } catch (error: any) {
    MessagePlugin.error(error?.message || '共享失败，请稍后重试')
  }
}
const sharedView = computed(() => route.query.view === 'shared' || route.query.library === 'organization_private_shared')

async function loadCurrentConversation(platform: string, id: string, force = false) {
  const loadKey = `${platform}:${id}:${sharedView.value ? 'shared' : 'full'}`
  const existing = conversationLoads.get(loadKey)
  if (existing) return existing
  const request = (async () => {
  const hadChat = Boolean(store.findConversation(platform, id))
  loading.value = !hadChat
  loadError.value = null
  try {
    // Detail pages refresh their own conversation. A directory refresh here
    // can replace the in-memory snapshot while a provider returns a partial
    // page, briefly turning a valid route into "conversation not found".
    await store.loadConversation(platform as 'feishu' | 'wecom' | 'wechat', id, force, { shared: sharedView.value })
  } catch (error: any) {
    const initialCode = String(error?.code || error?.error?.code || '')
    if (!hadChat && initialCode === 'conversation_not_found') {
      await new Promise((resolve) => window.setTimeout(resolve, 600))
      try {
        await store.loadConversation(platform as 'feishu' | 'wecom' | 'wechat', id, true, { shared: sharedView.value })
      } catch (retryError: any) {
        loadError.value = retryError
      }
    } else {
      const code = String(error?.code || error?.error?.code || '')
      if (code === 'forbidden' || code === 'conversation_not_found') {
        loadError.value = error
      } else if (!hadChat) {
        loadError.value = error
      }
    }
  } finally {
    loading.value = false
  }
  })()
  conversationLoads.set(loadKey, request)
  void request.then(() => {
    if (conversationLoads.get(loadKey) === request) conversationLoads.delete(loadKey)
  }, () => {
    if (conversationLoads.get(loadKey) === request) conversationLoads.delete(loadKey)
  })
  return request
}
async function loadOlder() {
  try {
    await store.loadOlderConversation(sourceKey.value, conversationId.value, { shared: sharedView.value })
  } catch (error: any) {
    MessagePlugin.error(error?.message || '加载更早内容失败，请稍后重试')
  }
}
function scheduleConversationPoll() {
  if (pollTimer.value != null) window.clearTimeout(pollTimer.value)
  const delay = conversationHasActiveWork.value ? 5000 : 30000
  pollTimer.value = window.setTimeout(async () => {
    if (document.visibilityState === 'visible') {
      await loadCurrentConversation(sourceKey.value, conversationId.value, true)
    }
    scheduleConversationPoll()
  }, delay)
}
async function refreshConversationOnReturn() {
  if (document.visibilityState !== 'visible') return
  await loadCurrentConversation(sourceKey.value, conversationId.value, true)
  scheduleConversationPoll()
}
onMounted(() => {
  document.addEventListener('visibilitychange', refreshConversationOnReturn)
  window.addEventListener('focus', refreshConversationOnReturn)
  scheduleConversationPoll()
})
const errorCode = computed(() => String(loadError.value?.code || loadError.value?.error?.code || ''))
const errorTitle = computed(() => errorCode.value === 'forbidden' ? '你没有权限查看这个群聊' : errorCode.value === 'conversation_not_found' ? '找不到这个会话' : '会话加载失败')
const errorDescription = computed(() => errorCode.value === 'forbidden' ? '请联系组织管理员确认你的组织成员状态。' : errorCode.value === 'conversation_not_found' ? '' : loadError.value ? '请稍后重试。' : '')
watch([sourceKey, conversationId], async ([platform, id]) => {
  await loadCurrentConversation(platform, id, true)
  scheduleConversationPoll()
}, { immediate: true })
watch(conversationHasActiveWork, scheduleConversationPoll)
onBeforeUnmount(() => {
  if (pollTimer.value != null) window.clearTimeout(pollTimer.value)
  document.removeEventListener('visibilitychange', refreshConversationOnReturn)
  window.removeEventListener('focus', refreshConversationOnReturn)
})
</script>
<style scoped lang="less">.conversation-missing { display: grid; place-items: center; min-height: 360px; gap: 10px; color: var(--td-text-color-secondary); text-align: center; }.conversation-missing h3 { margin: 0; color: var(--td-text-color-primary); }.conversation-missing p { margin: 0; font-size: 12px; }.resume-dialog p { margin: 0 0 14px; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }.resume-dialog__warning { padding: 10px 12px; border-radius: 6px; color: var(--td-error-color) !important; background: var(--td-error-color-1); }.resume-dialog__note { display: flex; align-items: center; gap: 6px; margin-top: 14px; color: var(--td-text-color-placeholder); font-size: 11px; }</style>
