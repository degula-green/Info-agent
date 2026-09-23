<template>
  <section class="wk-page conversation-page">
    <div class="conversation-head">
      <div class="wk-breadcrumb">
        <button type="button" @click="emit('back')">知识库</button>
        <t-icon name="chevron-right" />
        <span>{{ sourceName(chat.source) }}</span>
        <t-icon name="chevron-right" />
        <span>{{ chat.name }}</span>
      </div>

      <div class="conversation-title-row">
        <div>
          <h1>{{ chat.name }}</h1>
          <p>{{ sourceName(chat.source) }} · {{ chat.isDirect ? '私聊' : `${chat.members} 位成员` }} · 最近同步 {{ chat.lastSync }}</p>
        </div>
        <span class="chat-status" :class="`chat-status--${chat.collectionStatus}`"><i />{{ statusLabel(chat.collectionStatus) }}</span>
        <t-button variant="outline" :theme="chat.collectionStatus === 'collecting' ? 'warning' : 'primary'" :disabled="chat.collectionStatus === 'detached'" @click="emit('toggle', chat)">
          <template #icon><t-icon :name="chat.collectionStatus === 'detached' ? 'stop-circle' : chat.collectionStatus === 'collecting' ? 'pause-circle' : 'play-circle'" /></template>
          {{ chat.collectionStatus === 'detached' ? '已解除接入' : chat.collectionStatus === 'collecting' ? '停止采集' : chat.collectionStatus === 'missing' || chat.collectionStatus === 'paused' ? '继续采集' : '开始采集' }}
        </t-button>
        <t-button v-if="chat.isDirect" variant="outline" theme="primary" :disabled="shareSelecting && !selectedCount" @click="shareConversation">
          <template #icon><t-icon :name="shareSelecting ? 'check' : 'share'" /></template>
          {{ shareSelecting ? `共享已选（${selectedCount}）` : '选择内容并共享' }}
        </t-button>
        <t-button v-if="chat.isDirect && shareSelecting" variant="text" @click="cancelShareSelection">取消选择</t-button>
      </div>
    </div>

    <div class="conversation-meta">
      <span><t-icon name="time" /> 历史起点：{{ chat.historyStart || '尚未设置' }}</span>
      <span><t-icon name="chat" /> {{ chat.messageCount ?? chat.messages.length }} 条消息</span>
      <span><t-icon name="file" /> {{ chat.attachmentCount ?? chat.files.length }} 个文件</span>
    </div>

    <div class="conversation-list wk-panel">
      <div class="conversation-list__tabs">
        <button type="button" class="conversation-tab conversation-tab--active">
          消息与文件 <span>（{{ items.length }}）</span>
        </button>
        <span class="conversation-list__hint">{{ collectionHint }}</span>
      </div>

      <div v-if="items.length" class="conversation-table" role="table" :aria-label="chat.isDirect ? '私聊消息和文件列表' : '群聊消息和文件列表'">
        <div class="conversation-table__head" role="row">
          <span>名称</span>
          <span>状态</span>
          <span>大小</span>
          <span>类型</span>
          <span>来源</span>
          <span>采集时间</span>
        </div>
        <button
          v-for="item in items"
          :key="item.key"
          type="button"
          class="conversation-row"
          :aria-label="`打开${item.kind === 'file' ? '文件' : '消息'}：${item.name}`"
          @click="openItem(item)"
        >
          <span class="conversation-cell conversation-cell--name" data-label="名称">
            <input
              v-if="chat.isDirect && shareSelecting"
              class="share-selection"
              type="checkbox"
              :checked="isSelected(item)"
              :aria-label="`选择${item.kind === 'file' ? '文件' : '消息'}：${item.name}`"
              @click.stop
              @change="toggleSelected(item)"
            />
            <t-icon :name="item.kind === 'file' ? 'file' : 'chat-bubble'" />
            <span>
              <strong>{{ item.name }}</strong>
              <small>{{ item.detail }}</small>
            </span>
          </span>
          <span class="conversation-cell" data-label="状态"><em class="conversation-status">{{ item.status }}</em></span>
          <span class="conversation-cell conversation-cell--muted" data-label="大小">{{ item.size }}</span>
          <span class="conversation-cell" data-label="类型"><em class="conversation-type">{{ item.type }}</em></span>
          <span class="conversation-cell" data-label="来源">{{ item.source }}</span>
          <span class="conversation-cell conversation-cell--muted" data-label="采集时间">{{ item.updatedAt }}</span>
        </button>
      </div>
      <div v-else class="wk-empty-inline">还没有采集到消息或文件</div>
    </div>

    <t-dialog
      v-model:visible="messageDialogVisible"
      :header="false"
      :footer="false"
      :close-btn="false"
      width="min(840px, calc(100vw - 32px))"
      dialog-class-name="info-message-dialog"
      placement="center"
      destroy-on-close
    >
      <article v-if="activeMessage" class="detail-modal" aria-labelledby="message-detail-title">
        <header class="detail-modal__header">
          <div class="detail-modal__heading">
            <h2 id="message-detail-title">消息详情</h2>
            <div class="detail-modal__meta">
              <span><t-icon name="chat-bubble" />消息</span>
              <span><t-icon name="user" />{{ activeMessage.sender }}</span>
              <span><t-icon name="time" />{{ activeMessage.time }}</span>
            </div>
          </div>
          <button type="button" class="detail-modal__close" aria-label="关闭消息详情" @click="messageDialogVisible = false">
            <t-icon name="close" />
          </button>
        </header>

        <main class="detail-modal__body">
          <div v-if="isRichMessage(displayMessageContent(activeMessage.content))" class="message-content message-content--rich" v-html="sanitizeHTML(displayMessageContent(activeMessage.content))"></div>
          <pre v-else class="message-content">{{ displayMessageContent(activeMessage.content) || '（空消息）' }}</pre>
        </main>

        <footer class="detail-modal__footer">
          <div class="detail-modal__status">
            <span class="status-dot" :class="statusTone(messageDisplayStatus(activeMessage))" />
            <span>{{ messageDisplayLabel(activeMessage) }}</span>
            <span class="record-id">消息 ID: {{ activeMessage.sourceMessageId || activeMessage.id }}</span>
          </div>
          <div class="detail-modal__actions">
            <t-button variant="outline" @click="downloadMessage">
              <template #icon><t-icon name="download" /></template>下载
            </t-button>
          </div>
        </footer>
      </article>
    </t-dialog>

    <t-dialog
      v-model:visible="fileDialogVisible"
      :header="false"
      :footer="false"
      :close-btn="false"
      width="min(92vw, 1720px)"
      dialog-class-name="info-file-dialog"
      placement="center"
      destroy-on-close
    >
      <article v-if="activeFile" class="detail-modal document-modal" aria-labelledby="file-detail-title">
        <header class="detail-modal__header document-modal__header">
          <div class="detail-modal__heading">
            <h2 id="file-detail-title" :title="activeFile.name">{{ activeFile.name }}</h2>
            <div class="detail-modal__meta">
              <span><t-icon name="file" />{{ fileTypeLabel(activeFile) }}</span>
              <span><t-icon name="data" />{{ activeFile.size }}</span>
              <span><t-icon name="user" />{{ activeFile.uploader || '未知发送人' }} · {{ activeFile.sentAt || '发送时间未知' }}</span>
              <span><t-icon name="time" />采集于 {{ activeFile.collectedAt || activeFile.uploadedAt || '尚未同步' }}</span>
            </div>
          </div>
          <button type="button" class="detail-modal__close" aria-label="关闭文档预览" @click="fileDialogVisible = false">
            <t-icon name="close" />
          </button>
        </header>

        <main class="document-modal__canvas">
          <InfoAttachmentPreview :file="activeFile" :active="fileDialogVisible" />
        </main>

        <footer class="detail-modal__footer document-modal__footer">
          <div class="detail-modal__status">
            <span class="status-dot" :class="statusTone(attachmentDisplayStatus(activeFile))" />
            <span>{{ fileStatus(activeFile) }}</span>
            <span class="record-id">文档 ID: {{ activeFile.documentId ?? activeFile.id }}</span>
          </div>
          <t-button variant="outline" :loading="fileDownloading" :disabled="activeFile.contentAccessRequired" @click="downloadFile">
            <template #icon><t-icon :name="activeFile.contentAccessRequired ? 'lock-on' : 'download'" /></template>{{ activeFile.contentAccessRequired ? '内容受保护' : '下载' }}
          </t-button>
        </footer>
      </article>
    </t-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { getKnowledgeAttachmentContent } from '@/api/info-knowledge'
import InfoAttachmentPreview from '@/components/InfoAttachmentPreview.vue'
import type { CollectionStatus, InfoChat, InfoFile, InfoMessage } from '@/mock'
import { sourceName } from '@/mock'
import { knowledgeDisplayLabel, mapKnowledgeDisplayStatus, type KnowledgeDisplayStatus } from '@/knowledge-mapping'
import { sanitizeHTML } from '@/utils/security'
import { isDisplayableTextMessage } from '@/utils/message-visibility'

type ConversationItem = {
  key: string
  kind: 'message' | 'file'
  name: string
  detail: string
  status: string
  size: string
  type: string
  source: string
  updatedAt: string
  message?: InfoMessage
  file?: InfoFile
}

const props = defineProps<{ chat: InfoChat }>()
const emit = defineEmits<{
  (event: 'back'): void
  (event: 'toggle', chat: InfoChat): void
  (event: 'toast', text: string): void
  (event: 'share', payload: { conversationId: string; messageIDs: string[]; attachmentIDs: string[] }): void
}>()

const chat = computed(() => props.chat)
const messageDialogVisible = ref(false)
const fileDialogVisible = ref(false)
const activeMessage = ref<InfoMessage | null>(null)
const activeFile = ref<InfoFile | null>(null)
const fileDownloading = ref(false)
const shareSelecting = ref(false)
const selectedMessageIDs = ref<string[]>([])
const selectedAttachmentIDs = ref<string[]>([])

const items = computed<ConversationItem[]>(() => [
    ...chat.value.messages.filter((message) => !isAttachmentOnlyMessage(message)).map((message) => ({
      key: `message-${message.id}`,
      kind: 'message' as const,
      name: messagePreview(message.content),
      detail: `${message.sender} · ${message.time}`,
      status: messageDisplayLabel(message),
      size: '-',
      type: '消息',
      source: sourceName(chat.value.source),
      updatedAt: message.collectedAt || '尚未同步',
    message,
  })),
  ...chat.value.files.map((file) => ({
      key: `file-${file.id}`,
      kind: 'file' as const,
      name: file.name,
      detail: `${file.uploader || '未知发送人'} · ${file.sentAt || '发送时间未知'}`,
      status: attachmentStatus(file),
      size: file.size,
      type: file.type,
      source: sourceName(chat.value.source),
    updatedAt: file.collectedAt || file.uploadedAt || '尚未同步',
    file,
  })),
].sort((a, b) => {
  const time = (item: ConversationItem) => item.message?.collectionTimestamp || item.file?.collectionTimestamp || ''
  return Date.parse(time(b)) - Date.parse(time(a))
}))

const collectionHint = computed(() => chat.value.collectionStatus === 'collecting'
  ? '持续采集中'
  : chat.value.collectionStatus === 'paused'
    ? '已停止采集，可重新选择起点'
    : chat.value.collectionStatus === 'detached'
      ? '已解除接入，不能继续采集'
    : chat.value.collectionStatus === 'missing'
      ? '飞书中已找不到该群聊，历史内容仍保留'
      : '尚未开始采集')

function statusLabel(status: CollectionStatus) { return status === 'collecting' ? '采集中' : status === 'paused' ? '已停止采集' : status === 'detached' ? '已解除接入' : status === 'missing' ? '群聊已不存在' : status === 'error' ? '采集异常' : '未开始' }
function openItem(item: ConversationItem) { if (item.kind === 'message' && item.message) openMessage(item.message); if (item.kind === 'file' && item.file) openFile(item.file) }
function openMessage(message: InfoMessage) { activeMessage.value = message; activeFile.value = null; messageDialogVisible.value = true }
function openFile(file: InfoFile) { activeFile.value = file; activeMessage.value = null; fileDialogVisible.value = true }
const selectedCount = computed(() => selectedMessageIDs.value.length + selectedAttachmentIDs.value.length)

function itemID(item: ConversationItem) {
  return item.kind === 'file' ? item.file?.id || '' : item.message?.id || ''
}

function isSelected(item: ConversationItem) {
  const id = itemID(item)
  return item.kind === 'file' ? selectedAttachmentIDs.value.includes(id) : selectedMessageIDs.value.includes(id)
}

function toggleSelected(item: ConversationItem) {
  const id = itemID(item)
  if (!id) return
  const target = item.kind === 'file' ? selectedAttachmentIDs : selectedMessageIDs
  target.value = target.value.includes(id) ? target.value.filter((value) => value !== id) : [...target.value, id]
}

function cancelShareSelection() {
  shareSelecting.value = false
  selectedMessageIDs.value = []
  selectedAttachmentIDs.value = []
}

function shareConversation() {
  if (!chat.value.isDirect) return
  if (!shareSelecting.value) {
    shareSelecting.value = true
    selectedMessageIDs.value = []
    selectedAttachmentIDs.value = []
    return
  }
  if (!selectedCount.value) return
  emit('share', { conversationId: chat.value.id, messageIDs: [...selectedMessageIDs.value], attachmentIDs: [...selectedAttachmentIDs.value] })
  cancelShareSelection()
}

function isRichMessage(content?: string | null) {
  return /<\/?(?:p|div|span|br|strong|b|em|i|a|img|ul|ol|li|table|thead|tbody|tr|td|th)\b/i.test(String(content || ''))
}

function messagePreview(content?: string | null) {
  const value = displayMessageContent(content)
  if (!value) return '（空消息）'
  if (isRichMessage(value)) return value.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
  return value
}

function isAttachmentOnlyMessage(message: InfoMessage) {
	if (!isDisplayableTextMessage(message.messageType, message.content)) return true
	const raw = String(message.content || '').trim()
  const visible = displayMessageContent(raw).trim()
  if (!visible) return true
  if (/^(?:merged and forwarded message|forwarded message|file name|filename)$/i.test(raw)) return true
  if (!message.attachments?.length) return false
  if (/^\s*\{[\s\S]*\}\s*$/.test(raw)) {
    try {
      const payload = JSON.parse(raw) as Record<string, unknown>
      const text = [payload.text, payload.title, payload.content]
        .find((value) => typeof value === 'string' && value.trim())
      if (text) return false
      if (Object.keys(payload).some((key) => /^(?:file|image)_(?:key|token|name)$/i.test(key) || /^filename$/i.test(key))) return true
    } catch { /* keep the conservative media check below */ }
  }
  // XML media bodies are retained for privacy/audit processing, but their
  // visible representation is already the separate attachment row.
  return /^<\s*(?:\?xml[^>]*>\s*)?<msg\b/i.test(raw)
    && ['image', 'file', 'mixed'].includes(String(message.messageType || '').toLowerCase())
}

function displayMessageContent(content?: string | null) {
  const value = String(content || '').trim().replace(/^(?:wxid_[A-Za-z0-9_-]+(?:@chatroom)?|[A-Za-z0-9_-]+@chatroom)\s*:\s*/i, '')
  if (/^\{[\s\S]*\}$/.test(value)) {
    try {
      const payload = JSON.parse(value) as Record<string, unknown>
      for (const key of ['text', 'title', 'content', 'description']) {
        const text = payload[key]
        if (typeof text === 'string' && text.trim()) return text.trim()
      }
      if (Object.keys(payload).some((key) => /^(?:file|image)_(?:key|token)$/i.test(key))) return ''
    } catch { /* fall through to the original content */ }
  }
  if (!/^<(?:\?xml|msg|appmsg)\b/i.test(value)) return value
  const decode = (text: string) => text
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  const fields: string[] = []
  for (const tag of ['title', 'des', 'description']) {
    const match = value.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)</${tag}>`, 'i'))
    if (match) {
      const text = decode(match[1])
      if (text && !fields.includes(text)) fields.push(text)
    }
  }
  return fields.join('\n')
}

function fileStatus(file: InfoFile) {
  return knowledgeDisplayLabel(attachmentDisplayStatus(file))
}

function attachmentStatus(file: InfoFile) {
  return fileStatus(file)
}

function attachmentDisplayStatus(file: InfoFile): KnowledgeDisplayStatus {
  return mapKnowledgeDisplayStatus({ contentStatus: file.documentStatus || file.parseStatus, ragStatus: file.vectorStatus, searchable: file.searchable })
}

function messageDisplayStatus(message: InfoMessage): KnowledgeDisplayStatus {
  if (message.vectorStatus === 'ready' || message.vectorStatus === 'succeeded') return 'searchable'
  if (message.vectorStatus === 'failed') return 'failed'
  return 'collected'
}

function messageDisplayLabel(message: InfoMessage) {
  return knowledgeDisplayLabel(messageDisplayStatus(message))
}

function statusTone(status?: KnowledgeDisplayStatus | null) {
  if (status === 'failed') return 'status-dot--error'
  if (status === 'searchable' || status === 'parsed') return 'status-dot--success'
  return 'status-dot--pending'
}

function fileTypeLabel(file: InfoFile) {
  const type = String(file.type || '').replace(/^\./, '')
  return (type || file.name.split('.').pop() || 'FILE').toUpperCase()
}

function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

function downloadMessage() {
  if (!activeMessage.value) return
  const blob = new Blob([activeMessage.value.content || ''], { type: 'text/plain;charset=utf-8' })
  saveBlob(blob, `消息-${activeMessage.value.id}.txt`)
}

async function downloadFile() {
  if (!activeFile.value || activeFile.value.contentAccessRequired || fileDownloading.value) return
  fileDownloading.value = true
  try {
    const blob = await getKnowledgeAttachmentContent(activeFile.value.id, true)
    saveBlob(blob, activeFile.value.name)
  } catch (error: any) {
    emit('toast', error?.message || '源文件下载失败，请稍后重试')
  } finally {
    fileDownloading.value = false
  }
}
</script>

<style lang="less" scoped>
.wk-page { width: min(1120px, 100%); margin: 0 auto; padding: 30px 34px 56px; }
.conversation-head { margin-bottom: 14px; }
.wk-breadcrumb { display: flex; align-items: center; gap: 4px; margin-bottom: 15px; color: var(--td-text-color-secondary); font-size: 12px; }
.wk-breadcrumb button { padding: 0; border: 0; color: var(--td-brand-color); background: transparent; cursor: pointer; }
.wk-breadcrumb svg { width: 13px; }
.conversation-title-row { display: flex; align-items: center; gap: 14px; }
.conversation-title-row > div { flex: 1; min-width: 0; }
.conversation-title-row h1 { margin: 0 0 6px; font-size: 24px; font-weight: 600; }
.conversation-title-row p { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; }
.chat-status { display: inline-flex; align-items: center; gap: 5px; color: var(--td-text-color-placeholder); font-size: 11px; white-space: nowrap; }
.chat-status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.chat-status--collecting { color: var(--td-success-color); }
.chat-status--paused { color: var(--td-warning-color); }
.chat-status--detached { color: var(--td-text-color-placeholder); }
.chat-status--missing { color: var(--td-error-color); }
.chat-status--error { color: var(--td-error-color); }
.conversation-meta { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 23px; color: var(--td-text-color-placeholder); font-size: 11px; }
.conversation-meta span { display: inline-flex; align-items: center; gap: 5px; }
.conversation-meta svg { width: 13px; }
.wk-panel { overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.conversation-list__tabs { display: flex; align-items: center; justify-content: space-between; min-height: 64px; padding: 0 20px; border-bottom: 1px solid var(--td-component-stroke); }
.conversation-tab { position: relative; align-self: stretch; padding: 0 4px; border: 0; color: var(--td-text-color-secondary); background: transparent; font-size: 16px; cursor: pointer; }
.conversation-tab::after { position: absolute; right: 0; bottom: 0; left: 0; height: 3px; border-radius: 2px 2px 0 0; background: transparent; content: ''; }
.conversation-tab--active { color: var(--td-text-color-primary); font-weight: 600; }
.conversation-tab--active::after { background: var(--td-brand-color); }
.conversation-tab span { color: var(--td-text-color-secondary); font-weight: 400; }
.conversation-list__hint { color: var(--td-text-color-placeholder); font-size: 11px; }
.conversation-table__head, .conversation-row { display: grid; grid-template-columns: minmax(300px, 2.2fr) .9fr .7fr .9fr 1.1fr 1fr; align-items: center; gap: 18px; }
.conversation-table__head { min-height: 58px; padding: 0 20px; color: var(--td-text-color-secondary); font-size: 12px; }
.conversation-row { width: 100%; min-height: 76px; padding: 12px 20px; border: 0; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-primary); background: var(--td-bg-color-container); text-align: left; cursor: pointer; }
.conversation-row:hover { background: var(--td-bg-color-container-hover); }
.conversation-cell { min-width: 0; overflow: hidden; color: var(--td-text-color-secondary); font-size: 12px; }
.conversation-cell--name { display: flex; align-items: center; gap: 11px; color: var(--td-text-color-primary); }
.share-selection { width: 16px; height: 16px; flex: 0 0 16px; accent-color: var(--td-brand-color); cursor: pointer; }
.conversation-cell--name > svg { flex: 0 0 21px; width: 21px; height: 21px; color: var(--td-brand-color); }
.conversation-cell--name > span { min-width: 0; }
.conversation-cell--name strong, .conversation-cell--name small { display: block; overflow: hidden; text-overflow: ellipsis; }
.conversation-cell--name strong { display: -webkit-box; max-height: 42px; white-space: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; font-size: 13px; line-height: 1.6; }
.conversation-cell--name small { margin-top: 3px; color: var(--td-text-color-placeholder); font-size: 11px; white-space: nowrap; }
.conversation-cell--muted { color: var(--td-text-color-placeholder); }
.conversation-status, .conversation-type { display: inline-flex; align-items: center; min-height: 25px; padding: 3px 9px; border-radius: 6px; font-style: normal; white-space: nowrap; }
.conversation-status { color: var(--td-success-color); background: var(--td-success-color-1); }
.conversation-type { color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); }
.wk-empty-inline { padding: 70px 20px; color: var(--td-text-color-placeholder); text-align: center; font-size: 12px; }
.detail-modal { display: flex; max-height: calc(100dvh - 48px); flex-direction: column; overflow: hidden; background: var(--td-bg-color-container); }
.detail-modal__header { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: space-between; gap: 24px; padding: 28px 32px 20px; border-bottom: 1px solid var(--td-component-stroke); }
.detail-modal__heading { min-width: 0; }
.detail-modal__heading h2 { margin: 0; overflow: hidden; color: var(--td-text-color-primary); font-size: 20px; font-weight: 600; line-height: 1.45; text-overflow: ellipsis; white-space: nowrap; }
.detail-modal__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; margin-top: 13px; color: var(--td-text-color-placeholder); font-size: 12px; }
.detail-modal__meta span { display: inline-flex; align-items: center; gap: 6px; }
.detail-modal__meta svg { width: 16px; height: 16px; }
.detail-modal__close { display: inline-flex; width: 44px; height: 44px; flex: 0 0 44px; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 4px; color: var(--td-text-color-placeholder); background: transparent; cursor: pointer; }
.detail-modal__close:hover { color: var(--td-text-color-primary); background: var(--td-bg-color-container-hover); }
.detail-modal__close:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
.detail-modal__close svg { width: 22px; height: 22px; }
.detail-modal__body { display: flex; min-height: 0; flex: 1 1 auto; flex-direction: column; padding: 20px 24px 18px; overflow: hidden; }
.message-content { width: 100%; max-height: min(68vh, 720px); color: var(--td-text-color-primary); font-size: 14px; line-height: 1.8; overflow: auto; overflow-wrap: anywhere; white-space: pre-wrap; }
.message-editor { width: 100%; }
.message-editor :deep(textarea) { max-height: min(68vh, 720px); overflow-y: auto; }
.detail-modal__footer { display: flex; min-height: 76px; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 20px; padding: 14px 24px; border-top: 1px solid var(--td-component-stroke); background: var(--td-bg-color-container); }
.detail-modal__status { display: flex; min-width: 0; align-items: center; gap: 9px; color: var(--td-text-color-secondary); font-size: 12px; }
.status-dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: var(--td-text-color-disabled); }
.status-dot--success { background: var(--td-success-color); }
.status-dot--error { background: var(--td-error-color); }
.status-dot--pending { background: var(--td-warning-color); }
.record-id { max-width: 360px; overflow: hidden; color: var(--td-text-color-placeholder); text-overflow: ellipsis; white-space: nowrap; }
.detail-modal__actions { display: flex; flex: 0 0 auto; gap: 10px; }
.document-modal { height: min(92dvh, 1080px); max-height: calc(100dvh - 32px); }
.document-modal__header { padding: 22px 28px 16px; }
.document-modal__canvas { display: flex; min-height: 0; flex: 1; align-items: center; justify-content: center; overflow: hidden; padding: 18px 20px 20px; background: #f4f5f7; }
.document-modal__canvas :deep(.attachment-preview) { min-height: 0; }
.document-modal__footer { min-height: 76px; }
:global(.info-message-dialog.t-dialog), :global(.info-file-dialog.t-dialog) { padding: 0; overflow: hidden; border-radius: 14px; }
:global(.info-message-dialog .t-dialog__body), :global(.info-file-dialog .t-dialog__body) { padding: 0; }
:global(.t-dialog__wrap:has(.info-message-dialog) .t-dialog__position), :global(.t-dialog__wrap:has(.info-file-dialog) .t-dialog__position) { display: flex; min-height: 100%; height: 100%; align-items: center; justify-content: center; box-sizing: border-box; }
@media (max-width: 850px) {
  .conversation-table__head { display: none; }
  .conversation-row { grid-template-columns: 1fr 1fr; gap: 10px 16px; padding: 15px 17px; }
  .conversation-cell--name { grid-column: 1 / -1; }
  .conversation-cell::before { display: block; margin-bottom: 3px; color: var(--td-text-color-placeholder); font-size: 10px; content: attr(data-label); }
  .conversation-cell--name::before { display: none; }
}
@media (max-width: 760px) {
  .wk-page { padding: 22px 16px 45px; }
  .conversation-title-row { align-items: flex-start; flex-wrap: wrap; }
  .conversation-title-row .chat-status { margin-left: auto; }
  .conversation-list__tabs { padding: 0 14px; }
  .conversation-tab { font-size: 14px; }
  .conversation-list__hint { display: none; }
  :global(.t-dialog__position:has(> .info-message-dialog)), :global(.t-dialog__position:has(> .info-file-dialog)) { width: 100vw; height: 100dvh; padding: 0; }
  :global(.info-message-dialog.t-dialog), :global(.info-file-dialog.t-dialog) { width: 100vw !important; max-width: 100vw; height: 100dvh; max-height: 100dvh; border-radius: 0; }
  .detail-modal, .document-modal { width: 100%; height: 100dvh; max-height: 100dvh; }
  .detail-modal__header { gap: 12px; padding: 20px 18px 14px; }
  .detail-modal__heading h2 { font-size: 18px; }
  .detail-modal__meta { gap: 10px 14px; margin-top: 10px; }
  .detail-modal__body { flex: 1; min-height: 0; padding: 18px 16px 16px; }
  .message-content { max-height: min(70vh, 620px); font-size: 14px; line-height: 1.75; }
  .message-editor :deep(textarea) { max-height: min(70vh, 620px); font-size: 14px; }
  .detail-modal__footer { min-height: 72px; flex-wrap: wrap; gap: 12px; padding: 12px 16px; }
  .detail-modal__status { max-width: 100%; flex-wrap: wrap; }
  .record-id { max-width: min(70vw, 320px); }
  .detail-modal__actions { margin-left: auto; }
  .document-modal__canvas { overflow: hidden; }
  .document-modal__footer { flex-wrap: nowrap; }
}
</style>
