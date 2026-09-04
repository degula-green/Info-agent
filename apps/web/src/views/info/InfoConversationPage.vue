<template>
  <InfoConversation v-if="chat" :chat="chat" @back="router.push(`/knowledge/${chat.source}`)" @toggle="toggleChat" @edit="saveEdit" @toast="toast" />
  <div v-else-if="loading" class="conversation-missing"><t-icon name="loading" size="28px" /><h3>正在加载会话</h3><p>正在从 Core 同步消息和附件。</p></div>
  <div v-else class="conversation-missing"><t-icon name="error-circle" size="28px" /><h3>找不到这个会话</h3><t-button theme="primary" @click="router.push('/knowledge')">返回知识库</t-button></div>

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
      <p>为「{{ pendingResumeChat.name }}」选择采集起点。留空则从现在开始。</p>
      <t-form-item label="采集开始时间"><t-input v-model="resumeStart" type="date" clearable /></t-form-item>
      <div class="resume-dialog__note"><t-icon name="info-circle" />再次开启时默认参考上次停止采集的时间。</div>
    </div>
  </t-dialog>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import InfoConversation from '@/components/InfoConversation.vue'
import type { InfoChat } from '@/mock'
import { normalizeSourceKey, useInfoKnowledgeStore } from '@/stores/infoKnowledge'
const route = useRoute(); const router = useRouter(); const store = useInfoKnowledgeStore()
const sourceKey = computed(() => normalizeSourceKey(String(route.params.platform)) || 'wechat')
const conversationId = computed(() => String(route.params.conversationId))
const chat = computed(() => store.findConversation(sourceKey.value, conversationId.value))
const loading = ref(true)
const pollTimer = ref<number | null>(null)
const resumeDialogVisible = ref(false)
const resumeLoading = ref(false)
const pendingResumeChat = ref<InfoChat | null>(null)
const resumeStart = ref('')

function dateInputValue(value?: string | null) { return value ? value.slice(0, 10) : '' }

async function toggleChat(current: any) {
  if (current.collectionStatus === 'collecting') {
    await store.pauseConversation(sourceKey.value, current.externalId || current.id)
    toast('已停止采集')
    return
  }
  pendingResumeChat.value = current as InfoChat
  resumeStart.value = dateInputValue(current.lastStoppedAt) || current.historyStart || ''
  resumeDialogVisible.value = true
}

async function confirmResume() {
  const current = pendingResumeChat.value
  if (!current || resumeLoading.value) return
  resumeLoading.value = true
  try {
    await store.accessSession(sourceKey.value, current.externalId || current.id, resumeStart.value || null)
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
function saveEdit(payload: { kind: string; chatId: string; recordId: string; content: string }) {
  if (payload.kind === '消息') store.updateMessage(payload.chatId, payload.recordId, payload.content)
  else store.updateFile(payload.chatId, payload.recordId, payload.content)
  toast('内容已更新')
}
function toast(text: string) { MessagePlugin.success(text) }
async function loadCurrentConversation(platform: string, id: string, force = false) {
  loading.value = true
  try {
    // The shell refreshes conversation summaries globally. Do not force a
    // second summary refresh here, because it would temporarily clear detail
    // messages while this page is loading them.
    await store.ensureSources(false)
    await store.loadConversation(platform as 'feishu' | 'wecom' | 'wechat', id, force)
  } finally {
    loading.value = false
  }
}
onMounted(async () => {
  await loadCurrentConversation(sourceKey.value, conversationId.value)
  pollTimer.value = window.setInterval(() => {
    void loadCurrentConversation(sourceKey.value, conversationId.value, true)
  }, 30000)
})
watch([sourceKey, conversationId], async ([platform, id]) => {
  await loadCurrentConversation(platform, id, true)
})
onBeforeUnmount(() => {
  if (pollTimer.value != null) window.clearInterval(pollTimer.value)
})
</script>
<style scoped lang="less">.conversation-missing { display: grid; place-items: center; min-height: 360px; gap: 10px; color: var(--td-text-color-secondary); text-align: center; }.conversation-missing h3 { margin: 0; color: var(--td-text-color-primary); }.conversation-missing p { margin: 0; font-size: 12px; }.resume-dialog p { margin: 0 0 14px; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }.resume-dialog__warning { padding: 10px 12px; border-radius: 6px; color: var(--td-error-color) !important; background: var(--td-error-color-1); }.resume-dialog__note { display: flex; align-items: center; gap: 6px; margin-top: 14px; color: var(--td-text-color-placeholder); font-size: 11px; }</style>
