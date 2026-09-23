<template>
  <InfoConversation v-if="chat && !loadError" :chat="chat" @back="router.push(backPath)" @toggle="toggleChat" @toast="toast" @share="shareSelected" @load-more="loadOlder" />
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
import { sharePrivateResources } from '@/api/info-knowledge'
import InfoConversation from '@/components/InfoConversation.vue'
import type { InfoChat } from '@/mock'
import { normalizeSourceKey, useInfoKnowledgeStore } from '@/stores/infoKnowledge'
const route = useRoute(); const router = useRouter(); const store = useInfoKnowledgeStore()
const sourceKey = computed(() => normalizeSourceKey(String(route.params.platform)) || 'wechat')
const conversationId = computed(() => String(route.params.conversationId))
const chat = computed(() => store.findConversation(sourceKey.value, conversationId.value))
const backPath = computed(() => {
  const requested = String(route.query.return || '')
  if (requested.startsWith('/knowledge/')) return requested
  return chat.value?.isDirect ? '/knowledge/personal/private' : `/knowledge/${chat.value?.source || sourceKey.value}`
})
const loading = ref(true)
const loadError = ref<any>(null)
const pollTimer = ref<number | null>(null)
const resumeDialogVisible = ref(false)
const resumeLoading = ref(false)
const pendingResumeChat = ref<InfoChat | null>(null)
const conversationLoads = new Map<string, Promise<void>>()

async function toggleChat(current: any) {
  if (current.collectionStatus === 'detached') return
  if (current.collectionStatus === 'collecting') {
    await store.pauseConversation(sourceKey.value, current.externalId || current.id)
    toast('已停止采集')
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
    await store.resumeConversation(sourceKey.value, current.id)
    await store.loadConversation(sourceKey.value, conversationId.value, true)
    resumeDialogVisible.value = false
    pendingResumeChat.value = null
    toast('已恢复采集')
  } catch (error: any) {
    MessagePlugin.error(error?.message || '无法恢复采集')
  } finally {
    resumeLoading.value = false
  }
}
function toast(text: string) { MessagePlugin.success(text) }
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
async function loadCurrentConversation(platform: string, id: string, force = false) {
  const loadKey = `${platform}:${id}`
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
    await store.loadConversation(platform as 'feishu' | 'wecom' | 'wechat', id, force)
  } catch (error) {
    loadError.value = error
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
    await store.loadOlderConversation(sourceKey.value, conversationId.value)
  } catch (error: any) {
    MessagePlugin.error(error?.message || '加载更早内容失败，请稍后重试')
  }
}
onMounted(() => {
  pollTimer.value = window.setInterval(() => {
    void loadCurrentConversation(sourceKey.value, conversationId.value, true)
  }, 30000)
})
const errorCode = computed(() => String(loadError.value?.code || loadError.value?.error?.code || ''))
const errorTitle = computed(() => errorCode.value === 'forbidden' ? '你没有权限查看这个群聊' : errorCode.value === 'conversation_not_found' ? '找不到这个会话' : '会话加载失败')
const errorDescription = computed(() => errorCode.value === 'forbidden' ? '请联系组织管理员确认你的组织成员状态。' : errorCode.value === 'conversation_not_found' ? '' : loadError.value ? '请稍后重试。' : '')
watch([sourceKey, conversationId], async ([platform, id]) => {
  await loadCurrentConversation(platform, id, true)
}, { immediate: true })
onBeforeUnmount(() => {
  if (pollTimer.value != null) window.clearInterval(pollTimer.value)
})
</script>
<style scoped lang="less">.conversation-missing { display: grid; place-items: center; min-height: 360px; gap: 10px; color: var(--td-text-color-secondary); text-align: center; }.conversation-missing h3 { margin: 0; color: var(--td-text-color-primary); }.conversation-missing p { margin: 0; font-size: 12px; }.resume-dialog p { margin: 0 0 14px; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }.resume-dialog__warning { padding: 10px 12px; border-radius: 6px; color: var(--td-error-color) !important; background: var(--td-error-color-1); }.resume-dialog__note { display: flex; align-items: center; gap: 6px; margin-top: 14px; color: var(--td-text-color-placeholder); font-size: 11px; }</style>
