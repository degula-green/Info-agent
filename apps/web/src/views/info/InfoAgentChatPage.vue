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

            <ol v-if="message.steps.length" class="agent-steps" aria-label="执行步骤">
              <li v-for="step in message.steps" :key="step.id" :class="`agent-step--${step.status}`">
                <span>{{ step.label }}</span>
              </li>
            </ol>

            <div v-if="message.answer" class="agent-answer__content" v-html="renderChatMarkdown(message.answer)" />

            <div v-if="message.citations.length" class="agent-citations" aria-label="回答来源">
              <div v-for="citation in message.citations" :key="citationKey(citation)" class="agent-citation">
                <t-icon :name="citation.url ? 'link' : 'file'" />
                <div>
                  <strong>{{ citation.title || citation.source || citation.evidence_id }}</strong>
                  <p v-if="citation.quote">{{ citation.quote }}</p>
                  <a v-if="citation.url" :href="citation.url" target="_blank" rel="noreferrer">{{ citation.url }}</a>
                </div>
              </div>
            </div>

            <div v-if="message.todo" class="agent-todo">
              <div class="agent-todo__title"><t-icon name="task-checked" />待办已创建</div>
              <strong>{{ message.todo.title }}</strong>
              <span>{{ message.todo.due_at || message.todo.due_expression || '未设置截止时间' }}</span>
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
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, reactive, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { renderChatMarkdown } from '@/utils/chatMarkdownRenderer'
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
type Citation = { evidence_id: string; quote?: string; title?: string; url?: string; source?: string; type?: string }
type TodoResult = { todo_id: string; title?: string; due_at?: string | null; due_expression?: string | null }
type Step = { id: string; label: string; status: string }
type AgentMessage = {
  id: string
  role: 'user' | 'agent'
  text?: string
  taskId?: string
  status?: ChatStatus
  statusText?: string
  steps: Step[]
  answer?: string
  citations: Citation[]
  error?: string
  todo?: TodoResult
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
const inputValues = reactive<Record<string, string>>({})
let activeController: AbortController | null = null

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
      message.status = event.event_type === 'step.failed' ? 'executing' : 'executing'
      message.statusText = event.event_type === 'step.started'
        ? `正在执行 ${payload.capability || '能力'}`
        : event.event_type === 'step.succeeded'
          ? `已完成 ${payload.capability || '步骤'}`
          : `步骤失败：${payload.capability || '能力'}`
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
  }
}

function citationKey(citation: Citation): string {
  return citation.evidence_id || citation.url || citation.title || citation.quote || 'citation'
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
  const userMessage: AgentMessage = { id: newMessageID(), role: 'user', text, steps: [], citations: [], lastEventId: 0 }
  const agentMessage = reactive<AgentMessage>({
    id: newMessageID(),
    role: 'agent',
    status: 'received',
    statusText: '正在创建任务',
    steps: [],
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
.agent-answer__content { color: var(--td-text-color-primary); font-size: 15px; line-height: 1.8; }
.agent-answer__content :deep(p) { margin: 0 0 10px; }
.agent-answer__content :deep(pre) { overflow-x: auto; padding: 10px 12px; border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }
.agent-citations { display: grid; gap: 8px; margin-top: 14px; }
.agent-citation { display: flex; gap: 9px; padding: 10px 12px; border: 1px solid var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-primary); }
.agent-citation > svg { flex: 0 0 auto; color: var(--td-brand-color); }
.agent-citation strong { display: block; font-size: 13px; }
.agent-citation p { margin: 4px 0 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.5; }
.agent-citation a { display: block; overflow: hidden; margin-top: 4px; color: var(--td-brand-color); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.agent-todo { display: grid; gap: 5px; margin-top: 14px; padding: 13px; border: 1px solid var(--td-brand-color-focus); border-radius: 9px; background: var(--td-brand-color-1); }
.agent-todo__title { display: flex; align-items: center; gap: 6px; color: var(--td-brand-color); font-size: 12px; }
.agent-todo strong { font-size: 15px; }
.agent-todo span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-approval, .agent-input-request { display: grid; gap: 11px; margin-top: 14px; padding: 14px; border: 1px solid var(--td-warning-color-3); border-radius: 9px; background: var(--td-warning-color-1); }
.agent-approval__header, .agent-approval__actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.agent-approval__header span { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.agent-approval__header small { color: var(--td-text-color-secondary); }
.agent-approval label, .agent-input-request label { display: grid; gap: 5px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-approval input, .agent-input-request input { width: 100%; min-height: 34px; padding: 6px 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); }
.agent-approval__arguments { max-height: 220px; overflow: auto; margin: 0; padding: 10px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); font-size: 12px; }
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
@media (max-width: 760px) { .agent-chat-page { min-height: 560px; } .agent-chat-scroll { padding: 24px 16px 185px; } .agent-welcome { padding-bottom: 80px; } .agent-welcome h1 { font-size: 28px; } .agent-user-bubble { max-width: 88%; font-size: 13px; } .agent-composer-area { padding: 0 12px 14px; } .agent-textarea :deep(.t-textarea__inner) { min-height: 100px; padding: 13px 14px 58px; font-size: 14px; } .agent-input-request { grid-template-columns: 1fr; } .agent-input-request button { grid-column: 1; } }
</style>
