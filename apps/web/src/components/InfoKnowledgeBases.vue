<template>
  <section class="wk-page kb-page">
    <div v-if="loadError" class="wk-error" role="alert"><t-icon name="error-circle" /><span>{{ loadError }}</span></div>
    <template v-if="!activeSource">
      <header class="kb-home-header">
        <h1>知识库</h1>
        <div class="kb-home-tools">
          <t-input v-model="query" class="kb-home-search" clearable placeholder="搜索知识库和文档内容...">
            <template #prefix-icon><t-icon name="search" /></template>
          </t-input>
          <div class="kb-home-stats" aria-label="知识库统计">
            <span><t-icon name="book-open" />{{ sources.length }} 个知识库</span>
            <span><t-icon name="chat" />{{ totalSessionCount }} 个会话</span>
          </div>
        </div>
      </header>

      <div class="kb-section-label"><span>平台知识库</span><small>固定接入的三个数据来源</small></div>
      <div v-if="filteredSources.length" class="kb-grid">
        <button v-for="source in filteredSources" :key="source.key" class="kb-card" type="button" :disabled="source.available === false" @click="openSource(source)">
          <div class="kb-card__head">
            <span class="kb-card__icon" :style="{ background: sourceColor[source.key] }">{{ source.name.slice(0, 1) }}</span>
            <t-tag :theme="source.bound ? 'success' : source.available === false ? 'warning' : 'default'" variant="light" size="small">{{ source.bound ? '已连接' : source.available === false ? '暂未开放' : '未绑定' }}</t-tag>
          </div>
          <div class="kb-card__name">{{ source.kbName }}</div>
          <p>{{ source.description }}<template v-if="source.lastError"> · {{ source.lastError }}</template></p>
          <div class="kb-card__foot">
            <span v-if="source.available === false"><t-icon name="lock-on" />当前未开放</span>
            <span v-else-if="source.bound"><t-icon name="chat" />{{ source.selectedConversationCount ?? source.chats.length }} 个已接入会话</span>
            <span v-else><t-icon name="lock-on" />绑定{{ source.name }}后开放</span>
            <t-icon name="chevron-right" />
          </div>
        </button>
      </div>
      <div v-else class="wk-empty wk-empty--small"><t-icon name="search" size="24px" /><p>没有匹配的知识库或内容</p></div>
    </template>

    <template v-else>
      <div class="kb-breadcrumb"><button type="button" @click="$emit('home')">知识库</button><t-icon name="chevron-right" /><span>{{ activeSource.kbName }}</span></div>
      <header class="kb-platform-header">
        <div>
          <h1>{{ activeSource.kbName }}</h1>
          <p>集中管理已接入的群聊和私人聊天会话。</p>
        </div>
        <t-button theme="primary" :disabled="!activeSource.bound || activeSource.available === false" @click="openAccessDialog"><template #icon><t-icon name="add" /></template>接入群聊</t-button>
      </header>

      <div v-if="activeSource.available === false" class="wk-empty">
        <t-icon name="pause" size="30px" /><h3>{{ activeSource.name }}暂未开放</h3><p>当前平台不支持采集消息。</p><t-button theme="primary" @click="$emit('profile')">查看连接器</t-button>
      </div>

      <div v-else-if="!activeSource.bound" class="wk-empty">
        <t-icon name="lock-on" size="30px" /><h3>{{ activeSource.name }}尚未绑定</h3><p>绑定连接器后才能选择该平台的会话。</p><t-button theme="primary" @click="$emit('profile')">绑定{{ activeSource.name }}</t-button>
      </div>

      <template v-else>
        <div class="kb-subhead">
          <div><h2>已接入会话</h2><p>选择会话后会生成对应知识空间，并可单独开启或暂停采集。</p></div>
          <span class="kb-session-count">{{ activeSource.selectedConversationCount ?? activeSource.chats.length }} 个会话</span>
        </div>

        <div v-if="activeSource.chats.length" class="chat-kb-grid">
          <article v-for="chat in activeSource.chats" :key="chat.id" class="chat-kb-card">
            <button type="button" class="chat-kb-card__main" @click="$emit('chat', chat)">
              <span class="chat-kb-card__icon" :style="{ background: sourceColor[chat.source] }"><t-icon :name="chat.isDirect ? 'user' : 'chat'" /></span>
              <span class="chat-kb-card__body">
                <strong>{{ chat.name }}</strong>
                <small>{{ chat.isDirect ? '私人聊天' : '群聊' }} · {{ chat.members }} 位成员 · {{ contentCount(chat) }} 条内容</small>
              </span>
              <t-icon name="chevron-right" class="chat-kb-card__arrow" />
            </button>
            <div class="chat-kb-card__footer">
              <div class="chat-kb-card__meta">
                <span class="chat-status" :class="`chat-status--${chat.collectionStatus}`"><i />{{ statusLabel(chat.collectionStatus) }}</span>
                <small><t-icon name="time" />{{ chat.lastSync }}</small>
                <div v-if="!chat.isDirect" class="chat-kb-card__collectors">
                  <span v-for="collector in activeCollectors(chat)" :key="collector.id" class="collector-chip" :class="`collector-chip--${collector.role}`">
                    <span class="collector-chip__label">{{ collector.role === 'primary' ? '主采集' : '协同采集' }} · {{ shortCollectorName(collector.collectorUserId) }}</span>
                    <small v-if="collectorTelemetry(collector)">{{ collectorTelemetry(collector) }}</small>
                    <button v-if="collector.status !== 'removed'" type="button" aria-label="移除采集者" @click.stop="removeCollector(chat, collector.id)"><t-icon name="close" /></button>
                  </span>
                </div>
              </div>
              <t-button v-if="chat.collectionStatus === 'collecting'" theme="warning" variant="outline" size="small" @click="pauseChat(chat)"><template #icon><t-icon name="pause-circle" /></template>停止采集</t-button>
              <t-button v-else-if="chat.collectionStatus === 'detached'" theme="default" variant="outline" size="small" disabled><template #icon><t-icon name="stop-circle" /></template>已解除接入</t-button>
              <t-button v-else theme="primary" variant="outline" size="small" @click="resumeChat(chat)"><template #icon><t-icon name="play-circle" /></template>{{ chat.collectionStatus === 'missing' || chat.collectionStatus === 'paused' ? '继续采集' : '开始采集' }}</t-button>
            </div>
          </article>
        </div>
        <div v-else class="wk-empty wk-empty--small"><t-icon name="chat" size="24px" /><p>还没有接入会话</p><t-button theme="primary" variant="outline" size="small" @click="openAccessDialog">接入群聊</t-button></div>
      </template>
    </template>

    <t-dialog v-model:visible="accessDialogVisible" :header="`接入${activeSource?.name || ''}会话`" :footer="false" width="560px">
      <div v-if="activeSource" class="access-dialog">
        <div class="access-dialog__intro">
          <p>选择当前账号可接入的群聊或私人聊天；已由组织成员接入的群聊可以加入协同采集。</p>
          <t-button size="small" variant="outline" :loading="accessLoading" @click="emit('refresh-access')">
            <template #icon><t-icon name="refresh" /></template>刷新群聊列表
          </t-button>
        </div>
        <t-input v-model="accessQuery" clearable placeholder="搜索群聊或联系人昵称" class="access-dialog__search">
          <template #prefix-icon><t-icon name="search" /></template>
        </t-input>
        <div v-if="accessLoading" class="access-dialog__loading"><t-loading size="small" text="正在刷新可接入会话..." /></div>
        <div v-else-if="filteredAvailableSessions.length" class="access-session-list">
          <button v-for="session in filteredAvailableSessions" :key="session.id" type="button" class="access-session" :disabled="discoveryAction(session) === 'attached'" @click="accessSession(session.id)">
            <span class="access-session__icon" :style="{ background: sourceColor[activeSource.key] }"><t-icon :name="session.isDirect ? 'user' : 'chat'" /></span>
            <span class="access-session__body"><strong>{{ session.name }}</strong><small>{{ session.isDirect ? '私人聊天' : '群聊' }} · {{ session.members }} 位成员 · {{ session.externalId }}</small></span>
            <span class="access-session__action"><t-icon :name="discoveryAction(session) === 'attached' ? 'check-circle' : discoveryAction(session) === 'join' ? 'user-add' : 'add'" />{{ discoveryAction(session) === 'attached' ? '已接入' : discoveryAction(session) === 'join' ? '加入协同采集' : '接入' }}</span>
          </button>
        </div>
        <div v-else class="access-dialog__empty"><t-icon :name="accessQuery.trim() ? 'search' : 'check-circle-filled'" size="24px" /><p>{{ accessQuery.trim() ? '没有匹配的会话' : '当前可接入会话已全部接入' }}</p></div>
      </div>
    </t-dialog>

    <t-dialog v-model:visible="collectDialogVisible" :header="pendingSession ? '设置采集起点' : '开启会话采集'" :confirm-btn="'开始采集'" :cancel-btn="'取消'" @confirm="confirmCollect">
      <div v-if="collectingChat || pendingSession" class="collect-dialog"><p v-if="collectingChat?.collectionStatus === 'missing'" class="collect-dialog__warning">飞书中暂时找不到这个群聊。确认开始采集时会再次检查群聊是否存在。</p><p>为「{{ collectingChat?.name || pendingSession?.name }}」选择采集起点。留空则默认回溯最近 7 天。</p><t-form-item label="采集开始时间"><t-input v-model="collectStart" type="datetime-local" :min="historyStartMin" :max="historyStartMax" clearable /></t-form-item><div class="collect-dialog__note"><t-icon name="info-circle" />历史起点最多回溯 7 天，再次开启时默认参考上次停止采集的时间。</div></div>
    </t-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CollectionStatus, InfoAvailableSession, InfoChat, InfoCollector, InfoSource } from '@/mock'
import { sourceColor } from '@/mock'
import { discoveryAction, isHistoryStartAllowed } from '@/knowledge-mapping'

const props = defineProps<{ sources: InfoSource[]; initialSourceKey?: InfoSource['key'] | null; accessLoading?: boolean; loadError?: string | null }>()
const loadError = computed(() => props.loadError || '')
const emit = defineEmits<{
  (event: 'profile'): void
  (event: 'home'): void
  (event: 'chat', chat: InfoChat): void
  (event: 'toast', text: string): void
  (event: 'open-source', key: InfoSource['key']): void
  (event: 'access', sourceKey: InfoSource['key'], sessionId: string, historyStart?: string | null): void
  (event: 'toggle', sourceKey: InfoSource['key'], sessionId: string): void
  (event: 'resume', sourceKey: InfoSource['key'], sessionId: string): void
  (event: 'remove-collector', sourceKey: InfoSource['key'], sessionId: string, collectorID: string): void
  (event: 'refresh-access'): void
}>()

const query = ref('')
const activeSource = ref<InfoSource | null>(null)
const accessDialogVisible = ref(false)
const accessQuery = ref('')
const collectDialogVisible = ref(false)
const collectingChat = ref<InfoChat | null>(null)
const pendingSession = ref<InfoAvailableSession | null>(null)
const collectStart = ref('')
function localDateTimeInput(value: Date | string) {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${year}-${month}-${day}T${hours}:${minutes}`
}
const historyStartMin = localDateTimeInput(new Date(Date.now() - 7 * 24 * 60 * 60 * 1000))
const historyStartMax = localDateTimeInput(new Date())

const filteredAvailableSessions = computed(() => {
  const source = activeSource.value
  const needle = accessQuery.value.trim().toLowerCase()
  if (!source || !needle) return source?.availableSessions || []
  return source.availableSessions.filter((session) => `${session.name} ${session.externalId || session.id}`.toLowerCase().includes(needle))
})

watch(() => [props.initialSourceKey, props.sources], ([key]) => {
  activeSource.value = key ? props.sources.find((source) => source.key === key) ?? null : null
}, { immediate: true })

const totalSessionCount = computed(() => props.sources.reduce((sum, source) => sum + source.chats.length, 0))
const filteredSources = computed(() => {
  const normalizedQuery = query.value.trim().toLowerCase()
  if (!normalizedQuery) return props.sources
  return props.sources.filter((source) => {
    const searchable = [
      source.name,
      source.kbName,
      source.description,
      ...source.chats.flatMap((chat) => [chat.name, ...chat.messages.map((message) => message.content), ...chat.files.map((file) => `${file.name} ${file.content}`)]),
    ].join(' ').toLowerCase()
    return searchable.includes(normalizedQuery)
  })
})

function contentCount(chat: InfoChat) { return (chat.messageCount ?? chat.messages.length) + (chat.attachmentCount ?? chat.files.length) }
function activeCollectors(chat: InfoChat) { return (chat.collectors || []).filter((collector) => collector.status !== 'removed') }
function shortCollectorName(value: string) { const text = String(value || '').trim(); return text.length > 12 ? `${text.slice(0, 8)}…` : text || '未知成员' }
function relativeTime(value?: string | null) {
  if (!value) return '未心跳'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未心跳'
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000))
  if (seconds < 60) return '刚刚心跳'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前心跳`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前心跳`
  return '较久未心跳'
}
function collectorTelemetry(collector: InfoCollector) {
  const parts: string[] = []
  if (collector.status === 'unavailable') parts.push('暂不可用')
  if (collector.agentOnline !== undefined) parts.push(collector.agentOnline ? 'Agent 在线' : 'Agent 离线')
  if (collector.lastHeartbeatAt) parts.push(relativeTime(collector.lastHeartbeatAt))
  if (collector.consecutiveFailures) parts.push(`连续失败 ${collector.consecutiveFailures} 次`)
  if (collector.lastError) parts.push(collector.lastError)
  return parts.join(' · ')
}
function openSource(source: InfoSource) { activeSource.value = source; emit('open-source', source.key) }
function statusLabel(status: CollectionStatus) { return status === 'collecting' ? '采集中' : status === 'paused' ? '已停止采集' : status === 'detached' ? '已解除接入' : status === 'missing' ? '群聊已不存在' : status === 'error' ? '采集异常' : '未开始' }
function pauseChat(chat: InfoChat) { emit('toggle', chat.source, chat.externalId || chat.id) }
function resumeChat(chat: InfoChat) { emit('resume', chat.source, chat.id) }
function removeCollector(chat: InfoChat, collectorID: string) { emit('remove-collector', chat.source, chat.id, collectorID) }
function openCollectDialog(chat: InfoChat) { pendingSession.value = null; collectingChat.value = chat; collectStart.value = localDateTimeInput(chat.lastStoppedAt || chat.historyStartAt || ''); collectDialogVisible.value = true }
function openAccessDialog() {
  accessQuery.value = ''
  accessDialogVisible.value = true
  emit('refresh-access')
}
function confirmCollect() {
  if (!activeSource.value) return
  if (collectStart.value && !isHistoryStartAllowed(collectStart.value)) {
    emit('toast', '采集起点必须在最近 7 天内，且不能晚于今天')
    return
  }
  const requestedStart = collectStart.value ? new Date(collectStart.value).toISOString() : null
  collectDialogVisible.value = false
  if (pendingSession.value) {
    emit('access', activeSource.value.key, pendingSession.value.externalId || pendingSession.value.id, requestedStart)
  } else if (collectingChat.value) {
    emit('access', collectingChat.value.source, collectingChat.value.externalId || collectingChat.value.id, requestedStart)
  }
  pendingSession.value = null
  collectingChat.value = null
}
function accessSession(sessionId: string) {
  if (!activeSource.value) return
  const session = activeSource.value.availableSessions.find((item) => String(item.externalId || item.id) === String(sessionId))
  if (session && discoveryAction(session) === 'attached') return
  accessDialogVisible.value = false
  if (session && discoveryAction(session) === 'join') {
    emit('access', activeSource.value.key, session.externalId || session.id, null)
    return
  }
  pendingSession.value = session || { id: sessionId, name: sessionId, members: 0 }
  collectingChat.value = null
  collectStart.value = ''
  collectDialogVisible.value = true
}
</script>

<style lang="less" scoped>
.wk-page { width: min(1240px, 100%); margin: 0 auto; padding: 34px 38px 60px; }
.wk-error { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; padding: 10px 12px; border: 1px solid var(--td-error-color-3); border-radius: 6px; color: var(--td-error-color); background: var(--td-error-color-1); font-size: 12px; }
.kb-home-header { margin-bottom: 38px; }.kb-home-header h1, .kb-platform-header h1 { margin: 0; color: var(--td-text-color-primary); font-size: 28px; font-weight: 600; line-height: 1.25; }.kb-home-tools { display: flex; align-items: center; justify-content: space-between; gap: 28px; margin-top: 24px; }.kb-home-search { width: min(520px, 100%); }.kb-home-search :deep(.t-input) { border-color: transparent; border-radius: 12px; background: var(--td-bg-color-secondarycontainer); box-shadow: none; }.kb-home-search :deep(.t-input:hover), .kb-home-search :deep(.t-input.t-is-focused) { border-color: var(--td-brand-color); background: var(--td-bg-color-container); }.kb-home-stats { display: flex; align-items: center; justify-content: flex-end; gap: 28px; color: var(--td-text-color-secondary); font-size: 13px; white-space: nowrap; }.kb-home-stats span { display: inline-flex; align-items: center; gap: 6px; }.kb-home-stats svg { width: 16px; height: 16px; color: var(--td-text-color-placeholder); }
.kb-section-label { display: flex; align-items: center; gap: 9px; margin-bottom: 16px; font-size: 15px; font-weight: 600; }.kb-section-label small { color: var(--td-text-color-placeholder); font-size: 12px; font-weight: 400; }.kb-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }.kb-card { min-height: 184px; padding: 19px; border: 1px solid var(--td-component-stroke); border-radius: 14px; background: var(--td-bg-color-container); color: var(--td-text-color-primary); text-align: left; cursor: pointer; transition: border-color .18s ease, box-shadow .18s ease, transform .18s ease; }.kb-card:hover { border-color: var(--td-brand-color); box-shadow: 0 9px 22px rgba(0, 0, 0, .06); transform: translateY(-2px); }.kb-card__head { display: flex; align-items: center; justify-content: space-between; }.kb-card__icon { display: inline-grid; place-items: center; width: 36px; height: 36px; border-radius: 8px; color: #fff; font-size: 13px; font-weight: 600; }.kb-card__name { margin-top: 19px; font-size: 16px; font-weight: 600; }.kb-card p { min-height: 37px; margin: 7px 0 14px; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.55; }.kb-card__foot { display: flex; align-items: center; justify-content: space-between; color: var(--td-text-color-placeholder); font-size: 11px; }.kb-card__foot span { display: inline-flex; align-items: center; gap: 5px; }.kb-card__foot svg { width: 14px; }
.kb-breadcrumb { display: flex; align-items: center; gap: 5px; margin-bottom: 18px; color: var(--td-text-color-secondary); font-size: 12px; }.kb-breadcrumb button { padding: 0; border: 0; color: var(--td-brand-color); background: transparent; font: inherit; cursor: pointer; }.kb-breadcrumb svg { width: 14px; }.kb-platform-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 34px; }.kb-platform-header p { margin: 8px 0 0; color: var(--td-text-color-secondary); font-size: 13px; }.kb-platform-header :deep(.t-button) { min-width: 108px; }
.kb-subhead { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 16px; }.kb-subhead h2 { margin: 0 0 6px; color: var(--td-text-color-primary); font-size: 18px; font-weight: 600; }.kb-subhead p { margin: 0; color: var(--td-text-color-secondary); font-size: 13px; }.kb-session-count { color: var(--td-text-color-placeholder); font-size: 12px; white-space: nowrap; }.chat-kb-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }.chat-kb-card { display: flex; min-height: 178px; flex-direction: column; overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 14px; background: var(--td-bg-color-container); transition: border-color .18s ease, box-shadow .18s ease, transform .18s ease; }.chat-kb-card:hover { border-color: var(--td-component-border); box-shadow: 0 9px 22px rgba(0, 0, 0, .05); transform: translateY(-2px); }.chat-kb-card__main { display: flex; align-items: flex-start; gap: 11px; width: 100%; padding: 19px 18px 14px; border: 0; color: var(--td-text-color-primary); background: transparent; text-align: left; cursor: pointer; }.chat-kb-card__main:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: -2px; }.chat-kb-card__icon, .access-session__icon { display: inline-grid; place-items: center; flex: 0 0 36px; width: 36px; height: 36px; border-radius: 8px; color: #fff; }.chat-kb-card__icon svg, .access-session__icon svg { width: 19px; height: 19px; }.chat-kb-card__body { flex: 1; min-width: 0; }.chat-kb-card__body strong, .chat-kb-card__body small { display: block; }.chat-kb-card__body strong { overflow: hidden; font-size: 15px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }.chat-kb-card__body small { display: -webkit-box; min-height: 37px; margin-top: 5px; overflow: hidden; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.55; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }.chat-kb-card__arrow { flex: 0 0 16px; width: 16px; margin-top: 7px; color: var(--td-text-color-placeholder); }.chat-kb-card__footer { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-top: auto; padding: 12px 18px 15px; border-top: 1px solid var(--td-component-stroke); }.chat-kb-card__meta { display: grid; min-width: 0; gap: 6px; }.chat-kb-card__meta small { display: inline-flex; align-items: center; gap: 4px; overflow: hidden; color: var(--td-text-color-placeholder); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.chat-kb-card__meta small svg { flex: 0 0 13px; width: 13px; }.chat-status { display: inline-flex; align-items: center; gap: 5px; color: var(--td-text-color-placeholder); font-size: 12px; white-space: nowrap; }.chat-status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }.chat-status--collecting { color: var(--td-success-color); }.chat-status--paused { color: var(--td-warning-color); }.chat-status--missing { color: var(--td-error-color); }.chat-kb-card__footer :deep(.t-button) { flex: 0 0 auto; }
.chat-kb-card__collectors { display: flex; flex-wrap: wrap; gap: 4px; max-width: 100%; }
.chat-status--detached { color: var(--td-text-color-placeholder); }.chat-status--error { color: var(--td-error-color); }
.collector-chip { display: inline-flex; align-items: center; gap: 3px; max-width: 100%; padding: 2px 5px 2px 6px; border: 1px solid var(--td-component-stroke); border-radius: 5px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 10px; line-height: 1.35; }
.collector-chip__label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.collector-chip small { flex: 0 1 auto; max-width: 22rem; overflow: hidden; color: var(--td-text-color-placeholder); text-overflow: ellipsis; white-space: nowrap; }
.collector-chip--primary { border-color: var(--td-brand-color-2); color: var(--td-brand-color-7); background: var(--td-brand-color-1); }
.collector-chip button { display: inline-grid; place-items: center; width: 14px; height: 14px; padding: 0; border: 0; border-radius: 3px; color: inherit; background: transparent; cursor: pointer; }
.collector-chip button:hover { background: var(--td-bg-color-container); }
.collector-chip button :deep(svg) { width: 11px; height: 11px; }
.wk-empty { display: grid; place-items: center; min-height: 330px; padding: 28px; border: 1px dashed var(--td-component-stroke); border-radius: 12px; color: var(--td-text-color-secondary); text-align: center; }.wk-empty h3 { margin: 12px 0 5px; color: var(--td-text-color-primary); font-size: 15px; }.wk-empty p { margin: 0 0 16px; font-size: 12px; }.wk-empty--small { min-height: 170px; gap: 10px; border-style: solid; }.wk-empty--small p { margin: 0; }
.access-dialog__intro { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }.access-dialog__intro p, .collect-dialog p { margin: 0; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }.access-dialog__intro :deep(.t-button) { flex: 0 0 auto; }.access-dialog__search { margin-bottom: 14px; }.access-dialog__loading { display: grid; min-height: 150px; place-items: center; }.access-session-list { display: grid; max-height: 430px; gap: 8px; overflow-y: auto; padding-right: 3px; }.access-session { display: flex; align-items: center; gap: 11px; width: 100%; min-height: 66px; padding: 11px; border: 1px solid var(--td-component-stroke); border-radius: 10px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); text-align: left; cursor: pointer; transition: border-color .15s ease, background .15s ease; }.access-session:hover { border-color: var(--td-brand-color); background: var(--td-bg-color-secondarycontainer); }.access-session__body { flex: 1; min-width: 0; }.access-session__body strong, .access-session__body small { display: block; }.access-session__body strong { overflow: hidden; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }.access-session__body small { margin-top: 4px; overflow: hidden; color: var(--td-text-color-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.access-session__action { display: inline-flex; align-items: center; gap: 3px; color: var(--td-brand-color); font-size: 12px; white-space: nowrap; }.access-session__action svg { width: 14px; }.access-dialog__empty { display: grid; min-height: 150px; place-items: center; color: var(--td-success-color); text-align: center; }.access-dialog__empty p { margin: 8px 0 0; color: var(--td-text-color-secondary); font-size: 13px; }.collect-dialog__note { display: flex; align-items: center; gap: 6px; margin-top: 16px; color: var(--td-text-color-placeholder); font-size: 11px; }
.access-session:disabled { cursor: default; opacity: .68; }.access-session:disabled:hover { border-color: var(--td-component-stroke); background: var(--td-bg-color-container); }.access-session:disabled .access-session__action { color: var(--td-success-color); }
@media (max-width: 920px) { .wk-page { padding-right: 28px; padding-left: 28px; }.chat-kb-grid, .kb-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.kb-home-stats { gap: 16px; } }
@media (max-width: 700px) { .wk-page { padding: 24px 16px 44px; }.kb-home-header { margin-bottom: 28px; }.kb-home-tools, .kb-platform-header { align-items: stretch; flex-direction: column; }.kb-home-search { width: 100%; }.kb-home-stats { justify-content: flex-start; }.chat-kb-grid, .kb-grid { grid-template-columns: 1fr; }.kb-subhead { align-items: flex-start; flex-direction: column; }.chat-kb-card__footer { align-items: flex-end; }.access-session__action { display: none; } }
</style>
