<template>
  <section ref="pageRef" class="agent-chat-page">
    <div ref="scrollRef" class="agent-chat-scroll">
      <div v-if="!messages.length" class="agent-welcome">
        <h1>开始新的对话</h1>
        <p>输入指令，Agent 会在后台规划、审批并执行</p>
      </div>

      <div v-else class="agent-transcript">
        <div v-for="message in messages" :key="message.id" :class="['agent-message', `agent-message--${message.role}`]">
          <div v-if="message.role === 'user'" class="agent-user-bubble">{{ message.text }}</div>
          <div v-else class="agent-answer">
            <div class="agent-status" :class="`agent-status--${message.status || 'running'}`">
              <span class="agent-status__dot" />
              <span>{{ message.statusText || statusLabel(message.status) }}</span>
            </div>

            <div v-if="primaryAnswer(message)" class="agent-answer__content" v-html="renderChatMarkdown(primaryAnswer(message))" />
            <div v-else-if="showsEmptyKnowledgeResult(message)" class="agent-empty-result">
              <strong>没有找到满足条件的内容。</strong>
              <span v-if="metadataCoverage(message) === 'partial'">部分历史元数据尚未补齐，结果可能不完整。</span>
            </div>

            <section v-if="visibleSourceItems(message).length" class="agent-sources" aria-label="回答来源">
              <div class="agent-sources__header">
                <span>来源 {{ visibleSourceItems(message).length }}</span>
                <span v-if="metadataCoverage(message) === 'partial'" class="agent-coverage-tag">部分历史元数据缺失</span>
              </div>
              <article v-for="source in visibleSourceItems(message)" :key="source.key" class="agent-source-card" @click="openSourceItem(source)">
                <div class="agent-source-card__body">
                  <strong>{{ source.title }}</strong>
                  <span>{{ source.meta }}</span>
                  <p v-if="source.preview">{{ source.preview }}</p>
                </div>
                <div class="agent-source-card__actions">
                  <span v-if="source.kind === 'attachment'">预览</span>
                  <span v-else-if="source.conversation_id">来源会话</span>
                </div>
              </article>
            </section>

            <div v-if="message.steps.length" class="agent-trace">
              <button type="button" class="agent-trace__toggle" @click="toggleTrace(message)">
                <t-icon name="chevron-right" :class="{ 'agent-trace__chevron--open': isTraceOpen(message) }" />
                查看执行过程
              </button>
              <ol v-if="isTraceOpen(message)" class="agent-trace__steps" aria-label="执行步骤">
                <li v-for="step in message.steps" :key="step.id" :class="`agent-step--${step.status}`">
                  <span>{{ displayStepLabel(step.label) }}</span>
                  <small>{{ stepStatusLabel(step.status) }}</small>
                </li>
              </ol>
            </div>

            <div v-if="message.todo" class="agent-todo">
              <div class="agent-todo__title"><t-icon name="task-checked" />待办已创建</div>
              <strong>{{ message.todo.title }}</strong>
              <span>{{ message.todo.due_at || message.todo.due_expression || '未设置截止时间' }}</span>
            </div>

            <div v-if="message.formResult" class="agent-form-result">
              <div class="agent-form-result__title">
                <t-icon name="check-circle" />
                {{ message.formResult.verified ? '已写入表格' : '已提交' }}
              </div>
              <strong>{{ message.formResult.range }}</strong>
              <span v-if="!message.formResult.verified">结果无法自动确认，请自行核对</span>
            </div>

            <div v-if="message.approval" class="agent-approval">
              <div class="agent-approval__header">
                <span><t-icon name="lock-on" />需要确认</span>
                <small>{{ message.approval.capability }}</small>
              </div>
              <template v-if="message.approval.capability === 'todo.create'">
                <label>
                  <span>标题</span>
                  <input v-model="approvalEditors[message.id].title" maxlength="200" />
                </label>
                <label>
                  <span>截止日期</span>
                  <input v-model="approvalEditors[message.id].dueDate" type="date" />
                </label>
              </template>
              <template v-else-if="message.approval.capability === 'form.apply'">
                <div class="agent-form">
                  <p class="agent-form__title">
                    {{ formDraft(message).title || '链接表单' }}
                  </p>
                  <p class="agent-form__summary">
                    {{ formSummary(message).total }} 字段 ·
                    {{ formSummary(message).filled }} 已填
                    <span v-if="formSummary(message).empty"> · {{ formSummary(message).empty }} 待补</span>
                    <span v-if="formDraft(message).target_cell"> · 写入 {{ formDraft(message).target_cell }}</span>
                  </p>
                  <p v-if="formDraft(message).missing?.length" class="agent-form__warning">
                    缺少：{{ formDraft(message).missing.join('、') }}
                  </p>
                  <div class="agent-form__fields">
                    <label v-for="(field, index) in formEditor(message)" :key="`${field.name}-${index}`">
                      <span>{{ field.name }}</span>
                      <input v-model="field.value" :disabled="message.submitting" />
                    </label>
                  </div>
                </div>
              </template>
              <pre v-else class="agent-approval__arguments">{{ JSON.stringify(message.approval.arguments, null, 2) }}</pre>
              <div class="agent-approval__actions">
                <button type="button" class="agent-button agent-button--primary" :disabled="message.submitting" @click="confirmApproval(message)">确认</button>
                <button type="button" class="agent-button" :disabled="message.submitting" @click="rejectApproval(message)">拒绝</button>
              </div>
            </div>

            <form v-if="message.inputRequest" class="agent-input-request" @submit.prevent="submitInput(message)">
              <label>
                <span>需要补充的信息</span>
                <small v-if="message.inputRequest.missing.length">{{ message.inputRequest.missing.join('、') }}</small>
                <input v-model="inputValues[message.id]" :disabled="message.submitting" placeholder="请输入补充内容" />
              </label>
              <button type="submit" class="agent-button agent-button--primary" :disabled="message.submitting || !(inputValues[message.id] || '').trim()">提交</button>
            </form>

            <p v-if="message.error" class="agent-error">{{ message.error }}</p>

            <button
              v-if="message.taskId && !isTerminalAgentTaskStatus(message.status || '') && message.status !== 'waiting_approval' && message.status !== 'waiting_input'"
              type="button"
              class="agent-cancel"
              @click="cancelTask(message)"
            >
              取消任务
            </button>
          </div>
        </div>
      </div>
    </div>

    <div class="agent-composer-area">
      <div class="agent-composer" :class="{ 'agent-composer--focused': focused }">
        <t-textarea
          ref="textareaRef"
          v-model="question"
          class="agent-textarea"
          :autosize="{ minRows: 3, maxRows: 8 }"
          :disabled="Boolean(activeTaskID)"
          placeholder="输入你的指令..."
          @focus="focused = true"
          @blur="focused = false"
          @keydown="handleTextareaKeydown"
        />
        <div class="agent-composer__controls">
          <div class="agent-composer__left">
            <button
              type="button"
              class="agent-attach"
              :class="{ disabled: Boolean(activeTaskID) }"
              :disabled="Boolean(activeTaskID)"
              aria-label="添加附件"
              @click="openFilePicker"
            >
              <t-icon name="upload" />
            </button>
            <input
              ref="fileInputRef"
              class="agent-file-input"
              type="file"
              accept=".pdf,.docx,.png,.jpg,.jpeg"
              :disabled="Boolean(activeTaskID)"
              @change="handleFileChange"
            />
            <span v-if="selectedFile" class="agent-file-chip">
              <t-icon name="file" />
              <span>{{ selectedFile.name }}</span>
              <button type="button" aria-label="移除附件" @click="clearFile"><t-icon name="close" /></button>
            </span>
            <span v-if="activeTaskID" class="agent-composer__hint">Agent 正在执行</span>
            <span v-else-if="uploading" class="agent-composer__hint">正在上传附件</span>
          </div>
          <button
            type="button"
            class="agent-send"
            :class="{ disabled: Boolean(activeTaskID) || uploading || !question.trim() }"
            :disabled="Boolean(activeTaskID) || uploading || !question.trim()"
            aria-label="发送"
            @click="sendMessage"
          >
            <t-icon name="arrow-up" />
          </button>
        </div>
      </div>
      <p class="agent-disclaimer">内容由 Agent 执行结果生成，仅供参考</p>
    </div>
  </section>

  <t-dialog
    v-model:visible="sourcePreviewVisible"
    attach="body"
    width="min(92vw, 1720px)"
    :footer="false"
    destroy-on-close
    header="文档预览"
    dialog-class-name="agent-source-preview-dialog"
    placement="center"
  >
    <article v-if="activeSourceFile" class="agent-source-preview">
      <main><InfoAttachmentPreview :file="activeSourceFile" :active="sourcePreviewVisible" /></main>
      <footer v-if="activeSourceTarget?.conversation_id">
        <span>来自 Agent 检索结果</span>
        <t-button variant="outline" @click="openActiveSourceConversation"><template #icon><t-icon name="chat" /></template>查看来源会话</t-button>
      </footer>
    </article>
  </t-dialog>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, reactive, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { renderChatMarkdown } from '@/utils/chatMarkdownRenderer'
import { listKnowledgeConversationAttachments } from '@/api/info-knowledge'
import InfoAttachmentPreview from '@/components/InfoAttachmentPreview.vue'
import type { InfoFile } from '@/mock'
import { navigateToKnowledgeSource } from '@/utils/knowledge-source-navigation'
import {
  approveAgentApproval,
  buildScheduleDraft,
  cancelAgentTask,
  composeEditedArguments,
  createAgentTask,
  dayOfISO,
  getAgentTask,
  isTerminalAgentTaskStatus,
  listAgentObservations,
  rejectAgentApproval,
  streamAgentTaskEvents,
  submitAgentTaskInput,
  uploadAgentAttachment,
  type AgentApproval,
  type AgentTaskEvent,
  type AgentObservation,
} from '@/api/info-agent'

type ChatStatus = string
type Citation = {
  evidence_id: string
  quote?: string
  title?: string
  url?: string
  source?: string
  type?: string
  resource_id?: string
  resource_type?: string
  conversation_id?: string | number | null
  conversation_name?: string
  message_id?: string | number | null
  attachment_id?: string | number | null
  platform?: string
  sender_name?: string
  sent_at?: string
  position?: Record<string, any> | null
}
type TodoResult = { todo_id: string; title?: string; due_at?: string | null; due_expression?: string | null }
type Step = { id: string; label: string; status: string }
type SourceInfo = {
  resource_id: string
  resource_type: string
  knowledge_item_id?: string | null
  title?: string | null
  file_name?: string | null
  sender_id?: string | null
  sender_name?: string | null
  conversation_id?: string | null
  conversation_name?: string | null
  conversation_platform?: string | null
  sent_at?: string | null
  preview?: string | null
  score?: number
}
type ContentChunk = { chunk_id: string; text: string; score?: number; position?: Record<string, any> | null }
type ContentResult = {
  resource_id: string
  resource_type: string
  title?: string | null
  sender_name?: string | null
  conversation_name?: string | null
  conversation_id?: string | null
  sent_at?: string | null
  chunks: ContentChunk[]
  best_score?: number
}
type SourceListBlock = {
  type: 'source_list'
  summary: string
  items: SourceInfo[]
  resource_ids: string[]
  metadata_coverage: 'complete' | 'partial'
}
type ContentResultsBlock = {
  type: 'content_results'
  results: ContentResult[]
  metadata_coverage: 'complete' | 'partial'
}
type AnswerBlock = {
  type: 'answer'
  text: string
  citations: Citation[]
}
type AgentResultBlock = SourceListBlock | ContentResultsBlock | AnswerBlock
type SourceItem = {
  key: string
  title: string
  meta: string
  preview?: string
  kind: 'attachment' | 'message' | 'source'
  resource_id?: string
  conversation_id?: string | number | null
  message_id?: string | number | null
  platform?: string
}
type AgentMessage = {
  id: string
  role: 'user' | 'agent'
  text?: string
  taskId?: string
  status?: ChatStatus
  statusText?: string
  steps: Step[]
  blocks: AgentResultBlock[]
  answer?: string
  citations: Citation[]
  error?: string
  todo?: TodoResult
  /** Receipt for a form.preview / form.apply step, shown after a write. */
  formResult?: { summary: string; range?: string; verified?: boolean }
  approval?: AgentApproval
  inputRequest?: { missing: string[] }
  submitting?: boolean
  lastEventId: number
}

const question = ref('')
const focused = ref(false)
const uploading = ref(false)
const selectedFile = ref<File | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const messages = ref<AgentMessage[]>([])
const scrollRef = ref<HTMLElement | null>(null)
const pageRef = ref<HTMLElement | null>(null)
const textareaRef = ref<{ focus?: () => void } | null>(null)
const activeTaskID = ref('')
const approvalEditors = reactive<Record<string, { title: string; dueDate: string }>>({})
/**
 * The editable field list of a form.apply draft, keyed by message id. The
 * owner's edits are what the confirm call sends back, so the values written
 * are the ones on screen, not the ones the Agent proposed.
 */
const formEditors = reactive<Record<string, Array<{ name: string; value: string }>>>({})
const inputValues = reactive<Record<string, string>>({})
const expandedTraces = reactive<Record<string, boolean>>({})
let activeController: AbortController | null = null
const router = useRouter()
const route = useRoute()
const sourcePreviewVisible = ref(false)
const activeSourceFile = ref<InfoFile | null>(null)
const activeSourceTarget = ref<SourceInfo | Citation | null>(null)

function newMessageID(): string {
  return globalThis.crypto?.randomUUID?.() || `message-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function statusLabel(status?: string): string {
  return ({
    received: '任务已接收',
    planning: '正在规划',
    ready: '计划已生成',
    executing: '正在执行',
    waiting_approval: '等待确认',
    waiting_input: '等待补充信息',
    succeeded: '任务已完成',
    failed: '任务失败',
    unknown: '外部结果未知',
    cancelled: '任务已取消',
  } as Record<string, string>)[status || ''] || '正在处理'
}

function approvalArguments(event: AgentTaskEvent): Record<string, any> {
  return event.payload?.arguments && typeof event.payload.arguments === 'object'
    ? event.payload.arguments
    : {}
}

function eventTaskID(event: AgentTaskEvent, fallback: string): string {
  return String(event.task_id || event.payload?.task_id || fallback)
}

function rememberEvent(message: AgentMessage, event: AgentTaskEvent): void {
  message.lastEventId = Math.max(message.lastEventId, Number(event.sequence || 0))
}

async function prepareApproval(message: AgentMessage, event: AgentTaskEvent): Promise<void> {
  const args = approvalArguments(event)
  const timezone = String(args.timezone || 'Asia/Shanghai')
  const approval: AgentApproval = {
    approval_id: String(event.payload?.approval_id || ''),
    task_id: eventTaskID(event, message.taskId || ''),
    plan_id: String(event.payload?.plan_id || ''),
    step_id: String(event.payload?.step_id || ''),
    capability: String(event.payload?.capability || ''),
    arguments: args,
    version: Number(event.payload?.version || 1),
    status: 'waiting_approval',
  }
  message.approval = approval
  approvalEditors[message.id] = {
    title: String(args.title || approval.capability || '待确认操作'),
    dueDate: typeof args.due_at === 'string' ? dayOfISO(args.due_at, timezone) : '',
  }
  if (approval.capability === 'form.apply') {
    formEditors[message.id] = formFieldsFrom(args.draft)
  }
  message.status = 'waiting_approval'
  message.statusText = '等待确认'
}

function addOrUpdateStep(message: AgentMessage, event: AgentTaskEvent): void {
  const stepID = String(event.payload?.step_id || event.payload?.step?.step_id || '')
  if (!stepID) return
  const label = String(event.payload?.capability || event.payload?.step?.capability || stepID)
  const current = message.steps.find((step) => step.id === stepID)
  const status = event.event_type === 'step.succeeded' ? 'succeeded' : event.event_type === 'step.failed' ? 'failed' : 'running'
  if (current) {
    current.label = label
    current.status = status
  } else {
    message.steps.push({ id: stepID, label, status })
  }
}

function handleTaskEvent(message: AgentMessage, event: AgentTaskEvent): boolean | void {
  rememberEvent(message, event)
  const payload = event.payload || {}
  switch (event.event_type) {
    case 'task.accepted':
      message.status = 'received'
      message.statusText = '任务已接收'
      break
    case 'task.planning':
      message.status = 'planning'
      message.statusText = '正在理解与规划'
      break
    case 'task.understanding':
      message.status = 'planning'
      message.statusText = payload.decision_source === 'laya' ? '意图识别完成' : payload.decision_source === 'llm' ? '意图识别完成（LLM）' : '意图识别完成'
      break
    case 'plan.created':
      message.status = 'ready'
      message.statusText = `计划已生成（${Array.isArray(payload.steps) ? payload.steps.length : 0} 步）`
      break
    case 'plan.replanned':
      message.status = 'planning'
      message.statusText = '计划已调整'
      break
    case 'planner.decision':
      message.statusText = payload.action ? `规划决策：${payload.action}` : '规划决策已更新'
      break
    case 'step.started':
    case 'step.succeeded':
    case 'step.failed':
      addOrUpdateStep(message, event)
      if (event.event_type === 'step.succeeded') {
        const preview = payload.result_preview as Record<string, unknown> | undefined
        const writtenRange = typeof preview?.written_range === 'string' ? preview.written_range : ''
        // A preview step is only a progress line; a write produces the receipt.
        if (writtenRange && typeof preview?.summary === 'string') {
          message.formResult = {
            summary: preview.summary,
            range: writtenRange,
            verified: Boolean(preview?.verified),
          }
        }
      }
      message.status = event.event_type === 'step.failed' ? 'executing' : 'executing'
      message.statusText = event.event_type === 'step.started'
        ? stepStartText(String(payload.capability || ''))
        : event.event_type === 'step.succeeded'
          ? previewSummary(payload.result_preview) || stepSucceededText(String(payload.capability || ''))
          : `步骤失败：${displayStepLabel(String(payload.capability || '能力'))}`
      break
    case 'task.waiting_approval':
      void prepareApproval(message, event)
      return false
    case 'task.waiting_input':
      message.status = 'waiting_input'
      message.statusText = '需要补充信息'
      message.inputRequest = { missing: Array.isArray(payload.missing_information) ? payload.missing_information.map(String) : [] }
      return false
    case 'task.preview_confirmed':
      message.todo = {
        todo_id: String(payload.todo_id || ''),
        title: typeof payload.title === 'string' ? payload.title : undefined,
        due_at: typeof payload.due_at === 'string' ? payload.due_at : null,
        due_expression: typeof payload.due_expression === 'string' ? payload.due_expression : null,
      }
      break
    case 'approval.rejected':
      message.status = 'failed'
      message.statusText = '审批已拒绝'
      message.error = '审批已拒绝，任务不会执行'
      return false
    case 'task.completed':
      message.status = 'succeeded'
      message.statusText = '任务已完成'
      if (typeof payload.answer === 'string') message.answer = payload.answer
      if (Array.isArray(payload.citations)) message.citations = payload.citations.map(mapCitation)
      message.blocks = blocksFromObservations([], message.answer, message.citations)
      void hydrateTerminal(message)
      return false
    case 'task.failed':
      message.status = payload.task_status === 'unknown' ? 'unknown' : 'failed'
      message.statusText = payload.task_status === 'unknown' ? '外部结果未知' : '任务失败'
      message.error = String(payload.error?.message || '任务执行失败')
      return false
    case 'task.cancelled':
      message.status = 'cancelled'
      message.statusText = '任务已取消'
      return false
  }
}

function mapCitation(value: any): Citation {
  return {
    evidence_id: String(value?.evidence_id || value?.source_id || ''),
    quote: typeof value?.quote === 'string' ? value.quote : undefined,
    title: typeof value?.title === 'string' ? value.title : undefined,
    url: typeof value?.url === 'string' ? value.url : undefined,
    source: typeof value?.source === 'string' ? value.source : undefined,
    type: typeof value?.type === 'string' ? value.type : undefined,
    resource_id: value?.resource_id == null ? undefined : String(value.resource_id),
    resource_type: typeof value?.resource_type === 'string' ? value.resource_type : undefined,
    conversation_id: value?.conversation_id ?? value?.source_conversation_id ?? null,
    conversation_name: typeof value?.conversation_name === 'string' ? value.conversation_name : undefined,
    message_id: value?.message_id ?? value?.source_message_id ?? null,
    attachment_id: value?.attachment_id ?? null,
    platform: typeof value?.platform === 'string' ? value.platform : typeof value?.source_platform === 'string' ? value.source_platform : undefined,
    sender_name: typeof value?.sender_name === 'string' ? value.sender_name : undefined,
    sent_at: typeof value?.sent_at === 'string' ? value.sent_at : undefined,
    position: value?.position && typeof value.position === 'object' ? value.position : null,
  }
}

function previewSummary(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const summary = (value as Record<string, unknown>).summary
  return typeof summary === 'string' ? summary.trim() : ''
}

function primaryAnswer(message: AgentMessage): string {
  const block = message.blocks.find((item) => item.type === 'answer')
  const text = (message.answer || (block?.type === 'answer' ? block.text : '') || '').trim()
  if (!text || text === '没有找到满足条件的内容。') return ''
  return text
}

function showsEmptyKnowledgeResult(message: AgentMessage): boolean {
  if (message.status !== 'succeeded') return false
  if (primaryAnswer(message) || message.todo || message.formResult || message.approval || message.inputRequest) return false
  return !visibleSourceItems(message).length
}

function metadataCoverage(message: AgentMessage): 'complete' | 'partial' {
  const block = message.blocks.find((item) => item.type === 'source_list' || item.type === 'content_results')
  return block && 'metadata_coverage' in block && block.metadata_coverage === 'partial' ? 'partial' : 'complete'
}

function visibleSourceItems(message: AgentMessage): SourceItem[] {
  const items = new Map<string, SourceItem>()
  if (message.citations.length) {
    for (const citation of message.citations) {
      const item = sourceItemFromCitation(citation)
      if (item) items.set(item.key, item)
    }
    return [...items.values()]
  }
  for (const block of message.blocks) {
    if (block.type !== 'source_list' || !block.items.length) continue
    for (const source of block.items) {
      const item = sourceItemFromSource(source)
      items.set(item.key, item)
    }
  }
  return [...items.values()]
}

function sourceItemFromCitation(citation: Citation): SourceItem | null {
  const key = citationKey(citation)
  const kind: SourceItem['kind'] = citation.resource_type === 'attachment' ? 'attachment' : citation.conversation_id ? 'message' : 'source'
  return {
    key,
    kind,
    title: displayTitleForCitation(citation),
    meta: [citation.sender_name, citation.conversation_name, formatAgentMoment(citation.sent_at)].filter(Boolean).join(' · '),
    preview: truncateText(citation.quote, 120),
    resource_id: citation.resource_id || (citation.attachment_id != null ? String(citation.attachment_id) : undefined),
    conversation_id: citation.conversation_id,
    message_id: citation.message_id,
    platform: citation.platform,
  }
}

function sourceItemFromSource(source: SourceInfo): SourceItem {
  const kind: SourceItem['kind'] = source.resource_type === 'attachment' ? 'attachment' : 'message'
  return {
    key: sourceKey(source),
    kind,
    title: displayTitleForSource(source),
    meta: sourceMeta(source),
    preview: truncateText(source.preview, 120),
    resource_id: source.resource_id,
    conversation_id: source.conversation_id,
    platform: source.conversation_platform || undefined,
  }
}

function displayTitleForCitation(citation: Citation): string {
  if (citation.resource_type === 'attachment') return citation.title || citation.source || '附件'
  if (citation.title && !looksLikeInternalID(citation.title)) return citation.title
  if (citation.quote) return truncateText(citation.quote, 42) || '知识库消息'
  return citation.conversation_name ? `${citation.conversation_name}消息` : '知识库消息'
}

function displayTitleForSource(source: SourceInfo): string {
  if (source.resource_type === 'attachment') return source.file_name || source.title || source.preview || '附件'
  if (source.title && !looksLikeInternalID(source.title)) return source.title
  if (source.preview) return truncateText(source.preview, 42) || '知识库消息'
  return source.conversation_name ? `${source.conversation_name}消息` : '知识库消息'
}

function looksLikeInternalID(value: string): boolean {
  const text = value.trim()
  return /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(text) || /^[0-9a-f]{20,}$/i.test(text)
}

function truncateText(value?: string | null, limit = 80): string | undefined {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (!text) return undefined
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

function isTraceOpen(message: AgentMessage): boolean {
  return expandedTraces[message.id] === true
}

function toggleTrace(message: AgentMessage): void {
  expandedTraces[message.id] = !isTraceOpen(message)
}

function displayStepLabel(label: string): string {
  return ({
    'knowledge.search_sources': '检索本地知识',
    'knowledge.search_content': '查找相关内容',
    'knowledge.answer': '整理回答',
    'web.fetch': '读取网页',
    'web.extract': '提取网页内容',
    'answer.compose': '整理回答',
    'todo.create': '创建待办',
  } as Record<string, string>)[label] || label
}

function stepStartText(capability: string): string {
  return ({
    'knowledge.search_sources': '正在检索本地知识',
    'knowledge.search_content': '正在查找相关内容',
    'knowledge.answer': '正在整理回答',
    'web.fetch': '正在读取网页',
    'web.extract': '正在提取网页内容',
    'answer.compose': '正在整理回答',
    'todo.create': '正在创建待办',
  } as Record<string, string>)[capability] || `正在执行 ${displayStepLabel(capability || '能力')}`
}

function stepSucceededText(capability: string): string {
  return ({
    'knowledge.search_sources': '本地知识检索完成',
    'knowledge.search_content': '相关内容查找完成',
    'knowledge.answer': '回答已生成',
    'web.fetch': '网页读取完成',
    'web.extract': '网页内容提取完成',
    'answer.compose': '回答已生成',
    'todo.create': '待办已创建',
  } as Record<string, string>)[capability] || `${displayStepLabel(capability || '步骤')}完成`
}

function stepStatusLabel(status: string): string {
  return ({ running: '进行中', succeeded: '已完成', failed: '失败' } as Record<string, string>)[status] || status
}

function citationKey(citation: Citation): string {
  return citation.evidence_id || citation.url || citation.title || citation.quote || 'citation'
}

function sourceKey(source: SourceInfo): string {
  return `${source.resource_type}:${source.resource_id}`
}

function sourceLabel(source: SourceInfo): string {
  return source.title || source.file_name || source.preview || source.resource_id
}

function sourceMeta(source: SourceInfo): string {
  return [source.sender_name, source.conversation_name, formatAgentMoment(source.sent_at)].filter(Boolean).join(' · ')
}

function resultMeta(result: ContentResult): string {
  return [result.sender_name, result.conversation_name, formatAgentMoment(result.sent_at)].filter(Boolean).join(' · ')
}

function citationMeta(citation: Citation): string {
  return [citation.sender_name, citation.conversation_name, formatAgentMoment(citation.sent_at)].filter(Boolean).join(' · ')
}

function formatAgentMoment(value?: string | null): string {
  const raw = String(value || '').trim()
  if (!raw) return ''
  const moment = new Date(raw)
  if (Number.isNaN(moment.getTime())) return raw
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Shanghai',
  }).format(moment)
}

function blocksFromObservations(
  observations: AgentObservation[],
  fallbackAnswer?: string,
  fallbackCitations?: Citation[],
): AgentResultBlock[] {
  const blocks: AgentResultBlock[] = []
  for (const observation of observations) {
    if (observation.status !== 'succeeded') continue
    const output = observation.output || {}
    if (observation.capability === 'knowledge.search_sources' && Array.isArray(output.sources)) {
      blocks.push({
        type: 'source_list',
        summary: String(output.summary || ''),
        items: output.sources as SourceInfo[],
        resource_ids: Array.isArray(output.resource_ids) ? output.resource_ids.map(String) : [],
        metadata_coverage: output.metadata_coverage === 'partial' ? 'partial' : 'complete',
      })
    } else if (observation.capability === 'knowledge.search_content' && Array.isArray(output.results)) {
      blocks.push({
        type: 'content_results',
        results: output.results as ContentResult[],
        metadata_coverage: output.metadata_coverage === 'partial' ? 'partial' : 'complete',
      })
    } else if (observation.capability === 'knowledge.answer' && typeof output.answer === 'string') {
      blocks.push({
        type: 'answer',
        text: output.answer,
        citations: Array.isArray(output.citations) ? output.citations.map(mapCitation) : [],
      })
    }
  }
  if (!blocks.some((block) => block.type === 'answer') && fallbackAnswer) {
    blocks.push({
      type: 'answer',
      text: fallbackAnswer,
      citations: fallbackCitations || [],
    })
  }
  return blocks
}

function evidenceForObservations(observations: AgentObservation[]): Map<string, Citation> {
  const found = new Map<string, Citation>()
  for (const observation of observations) {
    const evidence = observation.output?.evidence
    if (!Array.isArray(evidence)) continue
    for (const item of evidence) {
      if (!item || typeof item !== 'object') continue
      const citation = mapCitation(item)
      if (citation.evidence_id) found.set(citation.evidence_id, citation)
    }
  }
  return found
}

function todoFromObservations(observations: AgentObservation[]): TodoResult | undefined {
  for (const observation of observations) {
    const output = observation.output
    if (observation.status === 'succeeded' && output?.todo_id) {
      return {
        todo_id: String(output.todo_id),
        title: typeof output.title === 'string' ? output.title : undefined,
        due_at: typeof output.due_at === 'string' ? output.due_at : null,
        due_expression: typeof output.due_expression === 'string' ? output.due_expression : null,
      }
    }
  }
  return undefined
}

async function openSourceItem(source: SourceItem): Promise<void> {
  if (source.kind === 'attachment' && source.resource_id) {
    await openSource({
      resource_id: source.resource_id,
      resource_type: 'attachment',
      title: source.title,
      file_name: source.title,
      conversation_id: source.conversation_id ? String(source.conversation_id) : null,
    })
    return
  }
  if (!source.conversation_id) {
    MessagePlugin.info('该来源暂无可打开的详情')
    return
  }
  try {
    await navigateToKnowledgeSource(router, route, {
      platform: source.platform,
      conversationId: source.conversation_id,
      messageId: source.message_id,
    })
  } catch {
    MessagePlugin.error('无法定位来源会话，请稍后重试')
  }
}

async function openSource(source: SourceInfo): Promise<void> {
  if (source.resource_type === 'attachment') {
    let name = source.file_name || source.title || '附件'
    let mimeType: string | undefined
    let contentAccessRequired = false
    let fileSizeBytes: number | null = null
    if (source.conversation_id) {
      try {
        const response = await listKnowledgeConversationAttachments(String(source.conversation_id))
        const metadata: any = (response.items || []).find((item: any) =>
          String(item.id || item.attachment_id || '') === String(source.resource_id)
          || String(item.external_attachment_id || '') === String(source.resource_id),
        )
        name = String(metadata?.file_name || metadata?.name || name)
        mimeType = String(metadata?.mime_type || metadata?.mimeType || '') || undefined
        contentAccessRequired = Boolean(metadata?.content_access_required)
        fileSizeBytes = Number(metadata?.size_bytes || 0) || null
      } catch {
        // The content endpoint still provides the definitive error state.
      }
    }
    activeSourceTarget.value = source
    activeSourceFile.value = {
      id: source.resource_id,
      name,
      type: name.includes('.') ? name.split('.').pop() || 'file' : 'file',
      mimeType,
      size: fileSizeBytes ? `${fileSizeBytes} bytes` : '-',
      time: '',
      uploadedAt: '',
      uploader: '',
      content: '',
      contentAccessRequired,
      fileSizeBytes,
      documentStatus: 'completed',
      parseStatus: 'completed',
    }
    sourcePreviewVisible.value = true
    return
  }
  await openSourceConversation(source)
}

async function openContentResult(result: ContentResult): Promise<void> {
  await openSource({
    resource_id: result.resource_id,
    resource_type: result.resource_type,
    title: result.title,
    file_name: result.title,
    conversation_name: result.conversation_name,
    sent_at: result.sent_at,
    conversation_id: result.conversation_id || null,
  })
}

async function openCitation(citation: Citation): Promise<void> {
  if (citation.resource_type === 'attachment') {
    await openContentResult({
      resource_id: String(citation.resource_id || citation.attachment_id || ''),
      resource_type: 'attachment',
      title: citation.title || citation.source,
      sender_name: citation.sender_name,
      conversation_name: citation.conversation_name,
      conversation_id: citation.conversation_id ? String(citation.conversation_id) : null,
      sent_at: citation.sent_at,
      chunks: [],
    })
    return
  }
  if (citation.conversation_id) {
    try {
      await navigateToKnowledgeSource(router, route, {
        platform: citation.platform,
        conversationId: citation.conversation_id,
        messageId: citation.message_id,
      })
    } catch {
      MessagePlugin.error('无法定位来源会话，请稍后重试')
    }
    return
  }
  MessagePlugin.info('该来源暂无可打开的详情')
}

async function openSourceConversation(source: SourceInfo | Citation): Promise<void> {
  if (!source.conversation_id) return
  try {
    await navigateToKnowledgeSource(router, route, {
      platform: platformForSource(source),
      conversationId: source.conversation_id,
      messageId: 'message_id' in source ? source.message_id : null,
      attachmentId: source.resource_type === 'attachment' ? source.resource_id : null,
    })
  } catch {
    MessagePlugin.error('无法定位来源会话，请稍后重试')
  }
}

function platformForSource(source: SourceInfo | Citation): string | undefined {
  return (source as SourceInfo).conversation_platform || (source as Citation).platform
}

async function openActiveSourceConversation(): Promise<void> {
  if (!activeSourceTarget.value) return
  await openSourceConversation(activeSourceTarget.value)
  sourcePreviewVisible.value = false
}

async function hydrateTerminal(message: AgentMessage): Promise<void> {
  if (!message.taskId) return
  try {
    const [task, observations] = await Promise.all([
      getAgentTask(message.taskId),
      listAgentObservations(message.taskId),
    ])
    if (!message.answer && typeof task.result?.answer === 'string') message.answer = task.result.answer
    const evidence = evidenceForObservations(observations)
    const citations = Array.isArray(task.result?.citations) ? task.result.citations : message.citations
    message.citations = citations.map((value: any) => {
      const citation = mapCitation(value)
      return evidence.get(citation.evidence_id) ? { ...citation, ...evidence.get(citation.evidence_id) } : citation
    })
    const blocks = blocksFromObservations(observations, message.answer, message.citations)
    if (blocks.length) message.blocks = blocks
    if (!message.todo) message.todo = todoFromObservations(observations)
    if (task.last_error?.message) message.error = String(task.last_error.message)
  } catch {
    // The terminal event already carries the user-visible result when available.
  }
  await scrollToBottom()
}

async function watchTask(message: AgentMessage, after = message.lastEventId): Promise<void> {
  if (!message.taskId) return
  activeController?.abort()
  activeController = new AbortController()
  activeTaskID.value = message.taskId
  try {
    await streamAgentTaskEvents(
      message.taskId,
      {
        onEvent: (event) => handleTaskEvent(message, event),
        onReconnect: (attempt) => {
          message.statusText = `正在恢复连接（${attempt}）`
        },
      },
      { after, signal: activeController.signal },
    )
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError')) {
      message.status = 'failed'
      message.statusText = '连接失败'
      message.error = error instanceof Error ? error.message : 'Agent 流式连接失败'
      MessagePlugin.error('Agent 流式连接失败')
    }
  } finally {
    if (activeTaskID.value === message.taskId) activeTaskID.value = ''
  }
  await scrollToBottom()
}

async function sendMessage(): Promise<void> {
  const text = question.value.trim()
  if (!text || activeTaskID.value || uploading.value) return
  const file = selectedFile.value
  let attachmentIds: string[] = []
  if (file) {
    uploading.value = true
    try {
      const uploaded = await uploadAgentAttachment(file)
      attachmentIds = [uploaded.attachment_id]
    } catch (error) {
      uploading.value = false
      MessagePlugin.error(error instanceof Error ? error.message : '附件上传失败')
      return
    }
    uploading.value = false
    selectedFile.value = null
    if (fileInputRef.value) fileInputRef.value.value = ''
  }
  const userMessage: AgentMessage = { id: newMessageID(), role: 'user', text, steps: [], blocks: [], citations: [], lastEventId: 0 }
  const agentMessage = reactive<AgentMessage>({
    id: newMessageID(),
    role: 'agent',
    status: 'received',
    statusText: '正在创建任务',
    steps: [],
    blocks: [],
    citations: [],
    lastEventId: 0,
  })
  messages.value.push(userMessage, agentMessage)
  question.value = ''
  try {
    const created = await createAgentTask({ text, attachmentIds })
    agentMessage.taskId = created.task_id
    agentMessage.status = created.status
    agentMessage.statusText = statusLabel(created.status)
    await watchTask(agentMessage, 0)
  } catch (error) {
    agentMessage.status = 'failed'
    agentMessage.statusText = '任务创建失败'
    agentMessage.error = error instanceof Error ? error.message : '无法创建 Agent Task'
    MessagePlugin.error(agentMessage.error)
  } finally {
    activeTaskID.value = ''
    await scrollToBottom()
  }
}

function handleFileChange(value: Event): void {
  const input = value.target as HTMLInputElement
  selectedFile.value = input.files?.[0] || null
}

function openFilePicker(): void {
  fileInputRef.value?.click()
}

function clearFile(): void {
  selectedFile.value = null
  if (fileInputRef.value) fileInputRef.value.value = ''
}

function approvalEditor(message: AgentMessage) {
  return approvalEditors[message.id] || { title: '', dueDate: '' }
}

function formFieldsFrom(draft: unknown): Array<{ name: string; value: string }> {
  const raw =
    draft && typeof draft === 'object' && Array.isArray((draft as Record<string, unknown>).fields)
      ? ((draft as Record<string, unknown>).fields as unknown[])
      : []
  return raw.map((item) => {
    const field = item && typeof item === 'object' ? (item as Record<string, unknown>) : {}
    return { name: String(field.name ?? ''), value: String(field.value ?? '') }
  })
}

function formEditor(message: AgentMessage) {
  return formEditors[message.id] || []
}

function formDraft(message: AgentMessage): Record<string, any> {
  const draft = message.approval?.arguments?.draft
  return draft && typeof draft === 'object' ? (draft as Record<string, any>) : {}
}

function formSummary(message: AgentMessage) {
  const fields = formEditor(message)
  const filled = fields.filter((field) => field.value.trim()).length
  return { total: fields.length, filled, empty: fields.length - filled }
}

async function confirmApproval(message: AgentMessage): Promise<void> {
  if (!message.approval || message.submitting) return
  message.submitting = true
  try {
    const approval = message.approval
    const editor = approvalEditor(message)
    let arguments_: Record<string, unknown> = approval.arguments || {}
    if (approval.capability === 'todo.create') {
      const draft = buildScheduleDraft({ task: { task_id: message.taskId || '', source_type: 'chat', owner_user_id: '', status: 'waiting_approval' }, approval })
      const edited = composeEditedArguments(draft, { title: editor.title, dueDate: editor.dueDate })
      arguments_ = { ...approval.arguments, ...edited.arguments, title: editor.title }
    }
    if (approval.capability === 'form.apply') {
      // Send back exactly what is on the card: the backend overwrites the Step
      // arguments with this and re-fingerprints before writing.
      const original = formDraft(message)
      const originalFields = Array.isArray(original.fields) ? (original.fields as Array<Record<string, any>>) : []
      const fields = formEditor(message).map((field, index) => {
        const before: Record<string, any> = originalFields[index] || {}
        const value = field.value.trim()
        return {
          ...before,
          name: field.name || String(before.name ?? ''),
          value: field.value,
          source: value ? (before.source && before.source !== 'empty' ? before.source : 'user') : 'empty',
          confidence: value ? 1 : 0,
        }
      })
      const draft: Record<string, any> = { ...original, fields }
      draft.missing = fields.filter((field) => !String(field.value || '').trim()).map((field) => String(field.name || ''))
      arguments_ = {
        ...approval.arguments,
        draft,
        values: [fields.map((field) => String(field.value || ''))],
        action: draft.write_model === 'live_document' ? 'write_cells' : approval.arguments?.action || 'write_cells',
      }
    }
    await approveAgentApproval(approval.approval_id, approval.version, arguments_)
    message.approval = undefined
    message.status = 'executing'
    message.statusText = '已确认，正在执行'
    message.lastEventId = Math.max(message.lastEventId, 0)
    await watchTask(message, message.lastEventId)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '确认失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function rejectApproval(message: AgentMessage): Promise<void> {
  if (!message.approval || message.submitting) return
  message.submitting = true
  try {
    await rejectAgentApproval(message.approval.approval_id, message.approval.version)
    message.approval = undefined
    message.status = 'failed'
    message.statusText = '审批已拒绝'
    message.error = '审批已拒绝，任务不会执行'
  } catch (error) {
    message.error = error instanceof Error ? error.message : '拒绝失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function submitInput(message: AgentMessage): Promise<void> {
  const value = (inputValues[message.id] || '').trim()
  if (!message.taskId || !value || message.submitting) return
  message.submitting = true
  try {
    await submitAgentTaskInput(message.taskId, { text: value })
    inputValues[message.id] = ''
    message.inputRequest = undefined
    message.status = 'planning'
    message.statusText = '已提交补充信息'
    await watchTask(message, message.lastEventId)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '补充信息提交失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function cancelTask(message: AgentMessage): Promise<void> {
  if (!message.taskId) return
  try {
    const task = await cancelAgentTask(message.taskId)
    message.status = task.status
    message.statusText = statusLabel(task.status)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '取消失败'
  }
}

function handleTextareaKeydown(value: unknown, context?: { e?: KeyboardEvent }): void {
  const event = context?.e || (value instanceof KeyboardEvent ? value : undefined)
  if (!event || event.key !== 'Enter' || (!event.ctrlKey && !event.metaKey)) return
  event.preventDefault()
  void sendMessage()
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  scrollRef.value?.scrollTo({ top: scrollRef.value.scrollHeight, behavior: 'smooth' })
}

onBeforeUnmount(() => activeController?.abort())
</script>

<style lang="less" scoped>
.agent-chat-page { position: relative; display: flex; flex-direction: column; width: 100%; height: 100%; min-height: 620px; background: var(--td-bg-color-container); }
.agent-chat-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 32px 34px 200px; }
.agent-welcome { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100%; padding-bottom: 120px; text-align: center; }
.agent-welcome h1 { margin: 0 0 12px; color: var(--td-text-color-primary); font-size: 36px; font-weight: 600; }
.agent-welcome p { margin: 0; color: var(--td-text-color-secondary); font-size: 16px; }
.agent-transcript { width: min(960px, 100%); margin: 0 auto; padding-bottom: 20px; }
.agent-message { display: flex; width: 100%; margin-bottom: 28px; }
.agent-message--user { justify-content: flex-end; }
.agent-user-bubble { max-width: min(620px, 75%); padding: 11px 15px; border-radius: 14px; color: var(--td-text-color-primary); background: var(--td-bg-color-secondarycontainer); font-size: 14px; line-height: 1.6; }
.agent-answer { width: min(760px, 100%); padding-left: 2px; }
.agent-status { display: inline-flex; align-items: center; gap: 7px; margin-bottom: 10px; color: var(--td-text-color-secondary); font-size: 13px; }
.agent-status__dot { width: 7px; height: 7px; border-radius: 50%; background: var(--td-brand-color); }
.agent-status--succeeded .agent-status__dot { background: var(--td-success-color); }
.agent-status--failed .agent-status__dot, .agent-status--unknown .agent-status__dot { background: var(--td-error-color); }
.agent-status--waiting_approval .agent-status__dot, .agent-status--waiting_input .agent-status__dot { background: var(--td-warning-color); }
.agent-steps { display: grid; gap: 5px; margin: 0 0 12px; padding: 0; list-style: none; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-steps li { display: flex; align-items: center; gap: 6px; }
.agent-steps li::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--td-brand-color-disabled); }
.agent-step--succeeded::before { background: var(--td-success-color) !important; }
.agent-step--failed::before { background: var(--td-error-color) !important; }
.agent-empty-result { display: grid; gap: 5px; margin-top: 4px; padding: 12px 13px; border: 1px dashed var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.6; }
.agent-empty-result strong { color: var(--td-text-color-primary); font-size: 13px; font-weight: 500; }
.agent-empty-result span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-sources { display: grid; gap: 8px; margin-top: 16px; }
.agent-sources__header { display: flex; align-items: center; gap: 8px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-coverage-tag { padding: 1px 7px; border-radius: 999px; color: var(--td-warning-color); background: var(--td-warning-color-1); font-size: 11px; }
.agent-trace { margin-top: 14px; }
.agent-trace__toggle { display: inline-flex; align-items: center; gap: 6px; padding: 0; border: 0; color: var(--td-text-color-secondary); background: transparent; font-size: 12px; cursor: pointer; }
.agent-trace__toggle svg { transition: transform .15s ease; }
.agent-trace__chevron--open { transform: rotate(90deg); }
.agent-trace__steps { display: grid; gap: 6px; margin: 9px 0 0; padding: 0; list-style: none; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-trace__steps li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 9px; border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }
.agent-trace__steps small { color: var(--td-text-color-placeholder); }
.agent-result-blocks { display: grid; gap: 14px; }
.agent-result-heading { display: flex; align-items: center; gap: 7px; margin-bottom: 9px; color: var(--td-text-color-primary); font-size: 13px; font-weight: 600; }
.agent-result-heading svg { color: var(--td-brand-color); }
.agent-empty-result { margin: 0; padding: 12px; border: 1px dashed var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-secondary); font-size: 13px; }
.agent-source-list, .agent-content-results { display: grid; gap: 8px; }
.agent-source-card, .agent-content-card { display: flex; justify-content: space-between; gap: 12px; padding: 12px; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.agent-source-card { cursor: pointer; transition: border-color .15s, background .15s; }
.agent-source-card:hover { border-color: var(--td-brand-color-focus); background: var(--td-bg-color-container-hover); }
.agent-source-card__body, .agent-content-card__title { min-width: 0; }
.agent-source-card strong, .agent-content-card strong { display: block; overflow: hidden; color: var(--td-text-color-primary); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.agent-source-card span, .agent-content-card span { display: block; margin-top: 4px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-source-card p { display: -webkit-box; overflow: hidden; margin: 7px 0 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.agent-source-card__actions, .agent-citation__actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; align-items: flex-start; gap: 6px; }
.agent-source-card__actions span { display: inline-block; min-height: 24px; margin: 0; padding: 0 8px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-brand-color); background: var(--td-bg-color-container); font-size: 12px; line-height: 22px; }
.agent-source-card__actions button, .agent-citation__actions button, .agent-citation__actions a { min-height: 28px; padding: 0 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-brand-color); background: var(--td-bg-color-container); font-size: 12px; line-height: 26px; text-decoration: none; cursor: pointer; }
.agent-content-card { display: block; }
.agent-content-card blockquote { margin: 9px 0 0; padding: 9px 10px; border-left: 3px solid var(--td-brand-color-focus); border-radius: 0 6px 6px 0; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 12px; line-height: 1.6; }
.agent-content-card .agent-source-card__actions { margin-top: 9px; }
.agent-coverage-note { margin: 2px 0 0; color: var(--td-warning-color); font-size: 12px; line-height: 1.5; }
.agent-answer-block { display: grid; gap: 14px; }
.agent-answer__content { color: var(--td-text-color-primary); font-size: 15px; line-height: 1.8; }
.agent-answer__content :deep(p) { margin: 0 0 10px; }
.agent-answer__content :deep(pre) { overflow-x: auto; padding: 10px 12px; border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }
.agent-citations { display: grid; gap: 8px; margin-top: 14px; }
.agent-citation { display: flex; gap: 9px; padding: 10px 12px; border: 1px solid var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-primary); }
.agent-citation > svg { flex: 0 0 auto; color: var(--td-brand-color); }
.agent-citation strong { display: block; font-size: 13px; }
.agent-citation p { margin: 4px 0 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.5; }
.agent-citation small { display: block; margin-top: 4px; color: var(--td-text-color-placeholder); font-size: 11px; }
.agent-citation a { display: block; overflow: hidden; margin-top: 4px; color: var(--td-brand-color); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.agent-citation__actions { margin-top: 7px; }
.agent-todo { display: grid; gap: 5px; margin-top: 14px; padding: 13px; border: 1px solid var(--td-brand-color-focus); border-radius: 9px; background: var(--td-brand-color-1); }
.agent-todo__title { display: flex; align-items: center; gap: 6px; color: var(--td-brand-color); font-size: 12px; }
.agent-todo strong { font-size: 15px; }
.agent-todo span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form-result { display: grid; gap: 4px; margin-top: 14px; padding: 12px 14px; border: 1px solid var(--td-success-color-3); border-radius: 9px; background: var(--td-success-color-1); }
.agent-form-result__title { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.agent-form-result span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-approval, .agent-input-request { display: grid; gap: 11px; margin-top: 14px; padding: 14px; border: 1px solid var(--td-warning-color-3); border-radius: 9px; background: var(--td-warning-color-1); }
.agent-approval__header, .agent-approval__actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.agent-approval__header span { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.agent-approval__header small { color: var(--td-text-color-secondary); }
.agent-approval label, .agent-input-request label { display: grid; gap: 5px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-approval input, .agent-input-request input { width: 100%; min-height: 34px; padding: 6px 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); }
.agent-approval__arguments { max-height: 220px; overflow: auto; margin: 0; padding: 10px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); font-size: 12px; }
.agent-form { display: grid; gap: 8px; }
.agent-form__title { margin: 0; font-weight: 600; }
.agent-form__summary { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form__warning { margin: 0; color: var(--td-warning-color-6, #e37318); font-size: 12px; }
.agent-form__fields { display: grid; gap: 8px; max-height: 260px; overflow: auto; }
.agent-form__fields label { display: grid; gap: 4px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form__fields input { width: 100%; min-height: 32px; padding: 6px 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); }
.agent-button { min-height: 34px; padding: 0 14px; border: 1px solid var(--td-component-stroke); border-radius: 7px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); cursor: pointer; }
.agent-button--primary { border-color: var(--td-brand-color); color: #fff; background: var(--td-brand-color); }
.agent-button:disabled { cursor: not-allowed; opacity: .55; }
.agent-input-request { grid-template-columns: 1fr auto; align-items: end; }
.agent-input-request label { grid-column: 1; }
.agent-input-request button { grid-column: 2; align-self: end; }
.agent-error { margin: 12px 0 0; color: var(--td-error-color); font-size: 13px; line-height: 1.6; }
.agent-cancel { margin-top: 12px; padding: 0; border: 0; color: var(--td-text-color-secondary); background: transparent; font-size: 12px; cursor: pointer; }
.agent-composer-area { position: absolute; z-index: 5; right: 0; bottom: 0; left: 0; display: flex; flex-direction: column; align-items: center; padding: 0 24px 22px; background: linear-gradient(to top, var(--td-bg-color-container) 62%, transparent); }
.agent-composer { position: relative; width: min(960px, 100%); border: 1px solid var(--td-component-stroke); border-radius: 14px; background: var(--td-bg-color-container); box-shadow: 0 2px 8px rgba(0, 0, 0, .04), 0 8px 16px -4px rgba(0, 0, 0, .06); transition: border-color .15s, box-shadow .15s; }
.agent-composer--focused { border-color: var(--td-brand-color); box-shadow: 0 0 0 3px var(--td-brand-color-focus), 0 8px 18px -8px rgba(0, 0, 0, .18); }
.agent-textarea :deep(.t-textarea__inner) { min-height: 112px; padding: 16px 18px 56px; border: 0; border-radius: 14px; resize: none; color: var(--td-text-color-primary); font-size: 16px; line-height: 1.5; box-shadow: none; }
.agent-textarea :deep(.t-textarea__inner:focus) { box-shadow: none; }
.agent-composer__controls { position: absolute; right: 14px; bottom: 12px; left: 14px; display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.agent-composer__left { display: flex; align-items: center; min-width: 0; gap: 8px; }
.agent-attach { display: grid; place-items: center; width: 30px; height: 30px; padding: 0; border: 0; border-radius: 8px; color: var(--td-text-color-secondary); background: transparent; cursor: pointer; }
.agent-attach:hover { color: var(--td-brand-color); background: var(--td-bg-color-secondarycontainer); }
.agent-attach:disabled { cursor: not-allowed; opacity: .5; }
.agent-file-input { position: absolute; width: 1px; height: 1px; overflow: hidden; opacity: 0; pointer-events: none; }
.agent-file-chip { display: inline-flex; align-items: center; max-width: 220px; gap: 5px; padding: 3px 8px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 12px; }
.agent-file-chip > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-file-chip button { display: grid; place-items: center; padding: 0; border: 0; color: inherit; background: transparent; cursor: pointer; }
.agent-composer__hint { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-send { display: grid; place-items: center; width: 38px; height: 38px; padding: 0; border: 0; border-radius: 50%; color: #fff; background: var(--td-brand-color); cursor: pointer; }
.agent-send.disabled { background: var(--td-brand-color-disabled); cursor: not-allowed; }
.agent-send svg { width: 21px; height: 21px; }
.agent-disclaimer { margin: 10px 0 0; color: var(--td-text-color-placeholder); font-size: 12px; }
.agent-source-preview { display: flex; height: min(76vh, 820px); min-height: 480px; flex-direction: column; gap: 10px; }
.agent-source-preview main { min-height: 0; flex: 1; }
.agent-source-preview footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 10px; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-secondary); font-size: 12px; }
.agent-source-preview-dialog .t-dialog__body { padding: 0; }
@media (max-width: 760px) { .agent-chat-page { min-height: 560px; } .agent-chat-scroll { padding: 24px 16px 185px; } .agent-welcome { padding-bottom: 80px; } .agent-welcome h1 { font-size: 28px; } .agent-user-bubble { max-width: 88%; font-size: 13px; } .agent-source-card { display: grid; } .agent-source-card__actions { justify-content: flex-start; } .agent-composer-area { padding: 0 12px 14px; } .agent-textarea :deep(.t-textarea__inner) { min-height: 100px; padding: 13px 14px 58px; font-size: 14px; } .agent-input-request { grid-template-columns: 1fr; } .agent-input-request button { grid-column: 1; } }
</style>
