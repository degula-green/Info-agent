<template>
  <InfoKnowledgeBases :sources="store.sources" :initial-source-key="sourceKey" :access-loading="store.loading" @profile="router.push('/profile')" @home="router.push('/knowledge')" @chat="openChat" @open-source="openSource" @access="accessSession" @toggle="pauseSession" @refresh-access="refreshAccessSessions" @toast="toast" />
</template>
<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import InfoKnowledgeBases from '@/components/InfoKnowledgeBases.vue'
import { normalizeSourceKey, useInfoKnowledgeStore } from '@/stores/infoKnowledge'
const route = useRoute(); const router = useRouter(); const store = useInfoKnowledgeStore(); const sourceKey = computed(() => normalizeSourceKey(String(route.params.platform)) || 'wechat')
function openChat(chat: { source: string; id: string }) { router.push(`/knowledge/${chat.source}/conversations/${chat.id}`) }
function openSource(key: string) { if (key !== sourceKey.value) router.push(`/knowledge/${key}`) }
function refreshAccessSessions() { void store.refreshAvailableSessions(sourceKey.value) }
async function accessSession(source: 'feishu' | 'wecom' | 'wechat', sessionId: string, historyStart?: string | null) {
  try {
    const chat = await store.accessSession(source, sessionId, historyStart || null)
    if (chat) toast(`已接入「${chat.name}」`)
  } catch (error: any) {
    MessagePlugin.error(error?.message || '无法开启采集')
  }
}
async function pauseSession(source: 'feishu' | 'wecom' | 'wechat', sessionId: string) {
  try {
    const chat = await store.pauseConversation(source, sessionId)
    if (chat) toast(`已停止「${chat.name}」的采集`)
  } catch (error: any) {
    MessagePlugin.error(error?.message || '无法停止采集')
  }
}
function toast(text: string) { MessagePlugin.success(text) }
onMounted(() => { void store.ensureSources() })
watch(() => sourceKey.value, () => { void store.ensureSources() }, { immediate: true })
</script>
