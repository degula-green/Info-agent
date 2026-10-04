import { authenticatedFetch } from '../auth/request.ts'
import { listConversations, type ConnectorPlatform } from './info-knowledge.ts'

const env = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(env.VITE_AGENT_BASE_URL || '/api/agent/v1').replace(/\/$/, '')

export class AgentApiError extends Error {
  code: string
  status: number
  retryable: boolean

  constructor(message: string, code = 'request_failed', status = 500, retryable = false) {
    super(message)
    this.name = 'AgentApiError'
    this.code = code
    this.status = status
    this.retryable = retryable
  }
}

function agentHeaders(): Headers {
  return new Headers({ Accept: 'application/json', 'Content-Type': 'application/json' })
}

async function agentRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await authenticatedFetch(`${baseURL}${path}`, { ...init, headers: agentHeaders() })
  const raw = await response.text()
  let body: any = null
  if (raw) {
    try {
      body = JSON.parse(raw)
    } catch {
      body = raw
    }
  }
  if (!response.ok) {
    throw new AgentApiError(
      response.status === 401 ? '登录已过期，请重新登录' : body?.message || body?.detail || `Agent request failed (${response.status})`,
      response.status === 401 ? 'AUTH_UNAUTHENTICATED' : body?.code || 'request_failed',
      response.status,
      Boolean(body?.retryable),
    )
  }
  return body as T
}

export interface AgentAttachmentUpload {
  attachment_id: string
  file_name: string
  mime_type: string
  size_bytes: number
}

async function agentMultipartRequest<T>(path: string, form: FormData): Promise<T> {
  // Never set Content-Type by hand here: the browser has to pick the multipart
  // boundary. The JSON helper above is therefore not reusable for uploads.
  // authenticatedFetch only adds the bearer token, so the boundary survives.
  const response = await authenticatedFetch(`${baseURL}${path}`, {
    method: 'POST',
    body: form,
    headers: new Headers({ Accept: 'application/json' }),
  })
  const raw = await response.text()
  let body: any = null
  if (raw) {
    try {
      body = JSON.parse(raw)
    } catch {
      body = raw
    }
  }
  if (!response.ok) {
    throw new AgentApiError(
      body?.message || body?.detail || `Agent upload failed (${response.status})`,
      body?.code || 'upload_failed',
      response.status,
      Boolean(body?.retryable),
    )
  }
  return body as T
}

export async function uploadAgentAttachment(file: File): Promise<AgentAttachmentUpload> {
  const data = new FormData()
  data.append('file', file)
  return agentMultipartRequest<AgentAttachmentUpload>('/attachments', data)
}

function uniqueClientMessageID(): string {
  return globalThis.crypto?.randomUUID?.() || `agent-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export interface AgentTaskCreated {
  task_id: string
  status: string
  events_url: string
  conversation_id?: string | null
}

export interface AgentTaskEvent {
  id: string
  sequence: number
  event_type: string
  task_id: string
  payload: Record<string, any>
  occurred_at?: string
}

export interface AgentTaskEventHandlers {
  onEvent: (event: AgentTaskEvent) => boolean | void
  onReconnect?: (attempt: number) => void
}

export interface AgentTaskEventStreamOptions {
  after?: number
  signal?: AbortSignal
  timeoutSeconds?: number
  maxReconnects?: number
}

export async function createAgentTask(input: {
  text: string
  conversationId?: string
  attachmentIds?: string[]
  clientMessageId?: string
  sourceRef?: Record<string, unknown>
  constraints?: Record<string, unknown>
}): Promise<AgentTaskCreated> {
  return agentRequest<AgentTaskCreated>('/tasks', {
    method: 'POST',
    body: JSON.stringify({
      text: input.text,
      attachment_ids: input.attachmentIds || [],
      source_type: 'chat',
      client_message_id: input.clientMessageId || uniqueClientMessageID(),
      ...(input.conversationId ? { conversation_id: input.conversationId } : {}),
      source_ref: input.sourceRef || {},
      constraints: input.constraints || {},
    }),
  })
}

function parseAgentTaskEvent(block: string): AgentTaskEvent | null {
  const lines = block.split(/\r?\n/)
  let id = ''
  let eventType = ''
  const data: string[] = []
  for (const line of lines) {
    if (line.startsWith('id:')) id = line.slice(3).trim()
    else if (line.startsWith('event:')) eventType = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (!data.length) return null
  try {
    const body = JSON.parse(data.join('\n'))
    const sequence = Number(body?.sequence ?? id ?? 0)
    const resolvedType = String(body?.event_type || eventType || 'message')
    return {
      id: String(body?.sequence ?? id ?? ''),
      sequence: Number.isFinite(sequence) ? sequence : 0,
      event_type: resolvedType,
      task_id: String(body?.task_id || ''),
      payload: body?.payload && typeof body.payload === 'object' ? body.payload : {},
      occurred_at: typeof body?.occurred_at === 'string' ? body.occurred_at : undefined,
    }
  } catch {
    return null
  }
}

export async function streamAgentTaskEvents(
  taskID: string,
  handlers: AgentTaskEventHandlers,
  options: AgentTaskEventStreamOptions = {},
): Promise<number> {
  const signal = options.signal
  const timeoutSeconds = Math.max(1, Number(options.timeoutSeconds || 30))
  const maxReconnects = Math.max(0, Number(options.maxReconnects ?? 20))
  let cursor = Math.max(0, Number(options.after || 0))
  let reconnects = 0

  while (!signal?.aborted) {
    try {
      const response = await authenticatedFetch(
        `${baseURL}/tasks/${encodeURIComponent(taskID)}/events?after=${cursor}&timeout_seconds=${timeoutSeconds}`,
        { headers: agentHeaders(), signal },
      )
      if (!response.ok || !response.body) {
        throw new AgentApiError(
          `Agent stream failed (${response.status})`,
          'stream_failed',
          response.status,
          response.status === 429 || response.status >= 500,
        )
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let stopped = false
      while (!stopped) {
        const chunk = await reader.read()
        if (chunk.done) break
        buffer += decoder.decode(chunk.value, { stream: true })
        const blocks = buffer.split(/\r?\n\r?\n/)
        buffer = blocks.pop() || ''
        for (const block of blocks) {
          const event = parseAgentTaskEvent(block)
          if (!event) continue
          cursor = Math.max(cursor, event.sequence)
          if (handlers.onEvent(event) === false) {
            stopped = true
            break
          }
        }
      }
      if (!stopped && buffer.trim()) {
        const event = parseAgentTaskEvent(buffer)
        if (event) {
          cursor = Math.max(cursor, event.sequence)
          stopped = handlers.onEvent(event) === false
        }
      }
      if (stopped) await reader.cancel().catch(() => undefined)
      if (stopped || signal?.aborted) return cursor

      if (reconnects >= maxReconnects) return cursor
      reconnects += 1
      handlers.onReconnect?.(reconnects)
      await wait(Math.min(250 * 2 ** (reconnects - 1), 2000), signal)
    } catch (error) {
      if (signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) return cursor
      if ((error as any)?.status === 401 || (error instanceof AgentApiError && !error.retryable)) throw error
      if (reconnects >= maxReconnects) throw error
      reconnects += 1
      handlers.onReconnect?.(reconnects)
      await wait(Math.min(250 * 2 ** (reconnects - 1), 2000), signal)
    }
  }
  return cursor
}

export function isTerminalAgentTaskStatus(status: string): boolean {
  return ['succeeded', 'failed', 'cancelled', 'unknown'].includes(status)
}

export interface AgentTask {
  task_id: string
  source_type: string
  owner_user_id: string
  status: string
  input?: Record<string, any>
  source_ref?: Record<string, any>
  objective?: string | null
  result?: { answer?: string; citations?: any[]; warnings?: string[] } | null
  last_error?: Record<string, any> | null
  conversation_id?: string | null
  request_message_id?: string | null
  response_message_id?: string | null
  created_at?: string
  updated_at?: string
}

export interface AgentConversationSummary {
  conversation_id: string
  title: string
  status: string
  last_message_at?: string | null
  message_count: number
}

export interface AgentConversationMessage {
  message_id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  status: string
  task_id?: string | null
  citations?: any[]
  client_message_id?: string | null
  created_at: string
  updated_at: string
}

export interface AgentConversationDetail {
  conversation_id: string
  owner_user_id: string
  organization_id?: string | null
  title: string
  status: string
  source: string
  summary?: string | null
  summary_cursor: number
  last_message_at?: string | null
  message_count: number
  messages: AgentConversationMessage[]
  created_at: string
  updated_at: string
}

export interface AgentConversationPage {
  items: AgentConversationSummary[]
  page: number
  page_size: number
  total: number
}

export async function listAgentConversations(
  page = 1,
  pageSize = 20,
): Promise<AgentConversationPage> {
  return agentRequest<AgentConversationPage>(
    `/conversations?page=${Math.max(1, page)}&page_size=${Math.max(1, pageSize)}`,
  )
}

export function getAgentConversation(conversationID: string): Promise<AgentConversationDetail> {
  return agentRequest<AgentConversationDetail>(
    `/conversations/${encodeURIComponent(conversationID)}`,
  )
}

export async function createAgentConversation(title = '新的对话'): Promise<AgentConversationSummary> {
  return agentRequest<AgentConversationSummary>('/conversations', {
    method: 'POST',
    body: JSON.stringify({ title }),
  })
}

export async function renameAgentConversation(
  conversationID: string,
  title: string,
): Promise<AgentConversationSummary> {
  return agentRequest<AgentConversationSummary>(
    `/conversations/${encodeURIComponent(conversationID)}`,
    {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    },
  )
}

export async function deleteAgentConversation(conversationID: string): Promise<void> {
  await agentRequest<void>(`/conversations/${encodeURIComponent(conversationID)}`, {
    method: 'DELETE',
  })
}

export interface AgentPlanStep {
  step_id: string
  plan_id: string
  order: number
  capability: string
  arguments: Record<string, any>
  status: string
}

export interface AgentPlan {
  plan_id: string
  task_id: string
  version: number
  objective: string
  steps: AgentPlanStep[]
  status: string
}

export interface AgentApproval {
  approval_id: string
  task_id: string
  plan_id: string
  step_id: string
  capability: string
  arguments: Record<string, any>
  version: number
  status: string
  reason?: string | null
  expires_at?: string | null
  created_at?: string
  /**
   * Whether the Agent can read the outcome back after the call. Resolved from
   * the capability registry; absent on older payloads.
   */
  reconcilable?: boolean
}

export interface AgentObservation {
  observation_id: string
  task_id: string
  plan_id: string
  step_id: string
  capability: string
  status: string
  output?: Record<string, any> | null
  error?: Record<string, any> | null
  created_at?: string
}

export type AgentTodoStatus = 'open' | 'done' | 'cancelled'

export interface AgentTodo {
  todo_id: string
  owner_user_id: string
  title: string
  due_at: string | null
  due_expression: string | null
  timezone: string | null
  notes: string | null
  status: AgentTodoStatus
  source?: Record<string, any>
  plan_id?: string | null
  step_id?: string | null
  created_at?: string
  updated_at?: string
  completed_at?: string | null
}

export interface AgentTodoUpdate {
  title?: string
  due_at?: string | null
  due_expression?: string | null
  timezone?: string | null
  notes?: string | null
  status?: AgentTodoStatus
}

// Drafts that still need the user. Everything else is not shown in the sidebar.
export const SCHEDULE_DRAFT_STATUSES = 'waiting_approval,waiting_input'

export async function listScheduleDrafts(limit = 20): Promise<AgentTask[]> {
  const body = await agentRequest<{ items?: AgentTask[] }>(
    `/tasks?status=${SCHEDULE_DRAFT_STATUSES}&limit=${limit}`,
  )
  return Array.isArray(body?.items) ? body.items : []
}

export function getAgentTask(taskID: string): Promise<AgentTask> {
  return agentRequest<AgentTask>(`/tasks/${encodeURIComponent(taskID)}`)
}

export async function getAgentPlan(taskID: string): Promise<AgentPlan | null> {
  const body = await agentRequest<{ plan?: AgentPlan | null }>(`/tasks/${encodeURIComponent(taskID)}/plan`)
  return body?.plan || null
}

export async function listAgentApprovals(): Promise<AgentApproval[]> {
  const body = await agentRequest<{ items?: AgentApproval[] }>('/approvals')
  return Array.isArray(body?.items) ? body.items : []
}

export async function listAgentObservations(taskID: string): Promise<AgentObservation[]> {
  const body = await agentRequest<{ items?: AgentObservation[] }>(
    `/tasks/${encodeURIComponent(taskID)}/observations`,
  )
  return Array.isArray(body?.items) ? body.items : []
}

/**
 * Reads the durable to-do ledger. The sidebar asks for open and done together
 * so the two tabs stay consistent on each poll; cancelled rows are never shown.
 */
export async function listAgentTodos(statuses: AgentTodoStatus[] = ['open']): Promise<AgentTodo[]> {
  const statusQuery = statuses.length
    ? `?status=${encodeURIComponent(statuses.join(','))}`
    : ''
  const body = await agentRequest<{ items?: AgentTodo[] }>(`/todos${statusQuery}`)
  return Array.isArray(body?.items) ? body.items : []
}

export function updateAgentTodo(
  todoID: string,
  changes: AgentTodoUpdate,
): Promise<AgentTodo> {
  return agentRequest<AgentTodo>(`/todos/${encodeURIComponent(todoID)}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

/** Soft completion: the row and its completion timestamp survive. */
export function completeAgentTodo(todoID: string): Promise<AgentTodo> {
  return updateAgentTodo(todoID, { status: 'done' })
}

/** Reserved for an explicit discard; completion must use completeAgentTodo. */
export async function deleteAgentTodo(todoID: string): Promise<void> {
  await agentRequest<null>(`/todos/${encodeURIComponent(todoID)}`, {
    method: 'DELETE',
  })
}

/**
 * Approves the pending write, optionally with the arguments the user edited on
 * the card.
 *
 * The backend overwrites the Step arguments with whatever is sent here and
 * re-fingerprints them before executing, so an edit is applied by the same call
 * that confirms it. Passing nothing keeps the plan-time arguments.
 */
export async function approveAgentApproval(
  approvalID: string,
  version: number,
  arguments_?: Record<string, unknown>,
): Promise<AgentApproval> {
  const body: Record<string, unknown> = { version }
  if (arguments_) body.arguments = arguments_
  return agentRequest<AgentApproval>(`/approvals/${encodeURIComponent(approvalID)}/approve`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function rejectAgentApproval(approvalID: string, version?: number): Promise<AgentApproval> {
  return agentRequest<AgentApproval>(`/approvals/${encodeURIComponent(approvalID)}/reject`, {
    method: 'POST',
    body: JSON.stringify(version == null ? {} : { version }),
  })
}

/**
 * Supplies the information a waiting Task asked for and lets the kernel re-plan
 * from the accumulated input. The Step-2 planner derives both the title and the
 * time phrase from `task.input.text`, so the fix is submitted as text.
 */
export async function submitAgentTaskInput(
  taskID: string,
  payload: { text?: string; fields?: Record<string, unknown>; resume?: boolean } = {},
): Promise<AgentTask> {
  return agentRequest<AgentTask>(`/tasks/${encodeURIComponent(taskID)}/input`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * Whether the Task is waiting on the owner to sign in, and where.
 *
 * The browser itself never leaves the sidecar; the Agent proxies both the
 * frame and the owner's clicks so the page stays off the public surface.
 */
export async function getAgentTakeover(taskID: string): Promise<{
  required: boolean
  url: string
  session_id: string
}> {
  return agentRequest(`/tasks/${encodeURIComponent(taskID)}/takeover`)
}

/**
 * One takeover frame, as an object URL.
 *
 * The endpoint needs the bearer token, so it is fetched and turned into a blob
 * rather than pointed at with an `<img src>`. The caller must revoke the URL.
 */
export async function fetchTakeoverFrame(taskID: string): Promise<string> {
  const response = await authenticatedFetch(
    `${baseURL}/tasks/${encodeURIComponent(taskID)}/takeover/screenshot`,
    { headers: agentHeaders() },
  )
  if (!response.ok) {
    throw new AgentApiError(`Takeover frame failed (${response.status})`, 'takeover_failed', response.status, response.status >= 500)
  }
  return URL.createObjectURL(await response.blob())
}

export type TakeoverInput =
  | { kind: 'click'; x: number; y: number }
  | { kind: 'type'; text: string }
  | { kind: 'key'; text: string }
  | { kind: 'scroll'; delta_x: number; delta_y: number }

export async function sendAgentTakeoverInput(taskID: string, input: TakeoverInput): Promise<void> {
  await agentRequest(`/tasks/${encodeURIComponent(taskID)}/takeover/input`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

/**
 * Whether the last write on this Task can be taken back.
 *
 * The browser service snapshots the cells just before it pastes, so an undo
 * restores exactly what the write replaced.
 */
export async function getFormUndo(taskID: string): Promise<{
  available: boolean
  written_range?: string
  target?: string
  restores_to?: string[][]
}> {
  return agentRequest(`/tasks/${encodeURIComponent(taskID)}/form/undo`)
}

export async function undoFormWrite(taskID: string): Promise<{
  reverted: boolean
  target: string
  verified: boolean
}> {
  return agentRequest(`/tasks/${encodeURIComponent(taskID)}/form/undo`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/**
 * Cancels a Task whose schedule has not been created yet. The Task becomes
 * terminal and leaves the waiting lists, so the card disappears for good.
 */
export async function cancelAgentTask(taskID: string): Promise<AgentTask> {
  return agentRequest<AgentTask>(`/tasks/${encodeURIComponent(taskID)}/cancel`, {
    method: 'POST',
  })
}

/**
 * Rewrites the collected sentence with the time the user supplied.
 *
 * Only the time phrase is swapped so the original wording (and therefore the
 * derived title) survives: "明天八点开会" + "明天晚上八点" -> "明天晚上八点开会".
 */
export function composeTimeFixText(rawText: string, timeExpression: string, replacement: string): string {
  const next = text(replacement)
  const base = text(rawText)
  if (!next) return base
  const previous = text(timeExpression)
  if (base && previous && base.includes(previous)) return base.replace(previous, next)
  // No known phrase to replace (for example a chat task): lead with the new time
  // so the planner finds it first and keeps the rest as the title.
  return base ? `${next} ${base}` : next
}

export type ScheduleDraftState =
  | 'confirmable'
  | 'needs_input'
  | 'creating'
  | 'created'
  | 'failed'

export interface ScheduleDraft {
  taskId: string
  taskStatus: string
  state: ScheduleDraftState
  title: string
  timeLabel: string
  /** The phrase the owner wrote ("下周"), kept so an edit can show it. */
  timeExpression?: string
  /** ISO instant of the deadline, when one resolved. */
  dueAt?: string
  /** Free-form note the planner attached to the to-do. */
  notes?: string
  /** True once the owner cleared the time: the to-do then has no deadline. */
  timeCleared?: boolean
  startTime?: string
  endTime?: string
  timezone?: string
  /** Display name of the message sender, when the source carries one. */
  senderLabel?: string
  sourceLabel: string
  sourceText: string
  approvalId?: string
  approvalVersion?: number
  /**
   * The pending argument payload, kept so an edit can be merged onto it.
   * Whatever the card sends back becomes the Step arguments verbatim.
   */
  arguments?: Record<string, unknown>
  missingInformation: string[]
  eventId?: string
  eventUrl?: string
  errorMessage?: string
  /**
   * Shown before the owner confirms an action whose outcome cannot be read back
   * afterwards. Absent for capabilities the Agent can reconcile.
   */
  verificationWarning?: string
}

const PLATFORM_LABELS: Record<string, string> = { feishu: '飞书', wechat: '微信', wecom: '企业微信' }
const CONVERSATION_LABELS: Record<string, string> = { group: '群聊', private: '私聊' }

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function formatMoment(value: unknown, timezone?: string): string {
  const raw = text(value)
  if (!raw) return ''
  const moment = new Date(raw)
  if (Number.isNaN(moment.getTime())) return raw
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium',
      timeStyle: 'short',
      ...(timezone ? { timeZone: timezone } : {}),
    }).format(moment)
  } catch {
    return moment.toLocaleString('zh-CN')
  }
}

const TODO_WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const TODO_WEEKDAY_INDEX: Record<string, number> = {
  Sun: 0,
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
}

interface CalendarDate {
  year: number
  month: number
  day: number
  weekday: number
}

function calendarDate(value: Date, timezone: string): CalendarDate | null {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone || 'Asia/Shanghai',
      weekday: 'short',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(value)
    const read = (type: Intl.DateTimeFormatPartTypes) =>
      parts.find((part) => part.type === type)?.value || ''
    const year = Number(read('year'))
    const month = Number(read('month'))
    const day = Number(read('day'))
    const weekday = TODO_WEEKDAY_INDEX[read('weekday')]
    if (!year || !month || !day || weekday === undefined) return null
    return { year, month, day, weekday }
  } catch {
    return null
  }
}

function calendarDayNumber(parts: CalendarDate): number {
  return Date.UTC(parts.year, parts.month - 1, parts.day) / 86_400_000
}

/** Compact deadline label for a real to-do row. */
export function agentTodoTimeLabel(todo: AgentTodo, now = new Date()): string {
  const raw = text(todo.due_at)
  if (!raw) return text(todo.due_expression) || '无时间'
  const due = new Date(raw)
  if (Number.isNaN(due.getTime())) return text(todo.due_expression) || '无时间'
  const zone = text(todo.timezone) || 'Asia/Shanghai'
  const dueDate = calendarDate(due, zone)
  const today = calendarDate(now, zone)
  if (!dueDate || !today) return formatMoment(raw, zone) || '无时间'
  const difference = calendarDayNumber(dueDate) - calendarDayNumber(today)
  if (difference === 0) return '今天'
  if (difference === 1) return '明天'
  const daysUntilSunday = (7 - today.weekday) % 7
  if (difference > 1 && difference <= daysUntilSunday) return TODO_WEEKDAYS[dueDate.weekday]
  const month = String(dueDate.month).padStart(2, '0')
  const day = String(dueDate.day).padStart(2, '0')
  return `${month}-${day}`
}

/** Human-readable provenance the ledger actually carries today. */
export function agentTodoSourceLabel(todo: AgentTodo): string {
  const source = todo.source || {}
  const sender = text(source.sender_display_name)
  if (sender) return sender
  const conversationType = text(source.conversation_type).toLowerCase()
  if (conversationType === 'group') return '群聊'
  if (conversationType === 'private') return '私聊'
  return '待办'
}

export function agentTodoSourceInitial(todo: AgentTodo): string {
  return Array.from(agentTodoSourceLabel(todo))[0] || '待'
}

/** Open items follow due time first; completed items follow creation time. */
export function sortAgentTodos(items: AgentTodo[], status: AgentTodoStatus): AgentTodo[] {
  return items
    .filter((item) => item.status === status)
    .sort((left, right) => {
      if (status === 'open') {
        const leftDue = left.due_at ? new Date(left.due_at).getTime() : Number.POSITIVE_INFINITY
        const rightDue = right.due_at ? new Date(right.due_at).getTime() : Number.POSITIVE_INFINITY
        if (leftDue !== rightDue) return leftDue - rightDue
      }
      return new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime()
    })
}

function sourceLabels(task: AgentTask): { label: string; conversation: string } {
  const ref = task.source_ref || {}
  const platform = text(ref.platform).toLowerCase()
  const conversationType = text(ref.conversation_type).toLowerCase()
  if (!platform) {
    // Chat-originated Tasks carry no source reference.
    return { label: task.source_type === 'chat' ? '对话' : '未知来源', conversation: '' }
  }
  return {
    label: PLATFORM_LABELS[platform] || platform || '未知来源',
    conversation: CONVERSATION_LABELS[conversationType] || '',
  }
}

function latestObservation(observations: AgentObservation[]): AgentObservation | null {
  if (!observations.length) return null
  return observations[observations.length - 1] || null
}

export interface BuildScheduleDraftInput {
  task: AgentTask
  approval?: AgentApproval | null
  plan?: AgentPlan | null
  observations?: AgentObservation[] | null
  /** Resolved from Knowledge so the card can name the source conversation. */
  conversationName?: string
  /** Set by the sidebar while a confirmation is in flight. */
  submitting?: boolean
}

export function buildScheduleDraft({
  task,
  approval,
  plan,
  observations,
  conversationName,
  submitting,
}: BuildScheduleDraftInput): ScheduleDraft {
  const arguments_ = approval?.arguments || plan?.steps?.[0]?.arguments || {}
  const timezone = text(arguments_.timezone) || text(task.source_ref?.timezone) || 'Asia/Shanghai'
  // A to-do carries `due_at`/`due_expression`; the retired calendar events used
  // `start_time`/`time_expression`. Both are read on purpose: the field names
  // are the only difference, so an old approval still renders.
  const dueAt = text(arguments_.due_at)
  const startTime = dueAt || text(arguments_.start_time)
  const endTime = text(arguments_.end_time)
  const timeExpression = text(arguments_.due_expression) || text(arguments_.time_expression)
  const startLabel = formatMoment(startTime, timezone)
  const endLabel = formatMoment(endTime, timezone)

  let timeLabel = ''
  if (startLabel) timeLabel = endLabel ? `${startLabel} → ${endLabel}` : startLabel
  else timeLabel = '未设置截止时间'

  const observation = latestObservation(observations || [])
  const missingInformation = Array.isArray(observation?.output?.missing_information)
    ? (observation?.output?.missing_information as unknown[]).map((item) => String(item))
    : []
  // todo.create reports the row it wrote, not a calendar event. Reading
  // event_id as well keeps a draft rendered from an old calendar approval
  // working, but the to-do id is the one that is actually populated now.
  const eventId = text(observation?.output?.todo_id) || text(observation?.output?.event_id)
  const eventUrl = text(observation?.output?.event_url)
  const observationError = observation?.status && observation.status !== 'succeeded' ? observation.error : null
  const errorMessage =
    text(observationError?.message) ||
    text(task.last_error?.message) ||
    (observation?.status === 'unknown' ? '外部结果未知，请联系管理员确认是否已创建' : '')

  // The Task status wins over the local "confirmation is in flight" hint: a Task
  // that already came back for the user must say what it needs instead of hiding
  // behind a spinner. Terminal statuses are final, waiting statuses are explicit,
  // and every other status means the kernel is still working on it.
  const cancelled = task.status === 'cancelled'
  const terminal = cancelled || ['succeeded', 'failed', 'unknown'].includes(task.status)
  let state: ScheduleDraftState
  if (task.status === 'succeeded') state = 'created'
  else if (terminal) state = 'failed'
  else if (task.status === 'waiting_input') {
    // The only capability left is todo.create, whose missing input is the time.
    state = 'needs_input'
  } else if (task.status === 'waiting_approval') {
    state = submitting ? 'creating' : 'confirmable'
  } else {
    // received / planning / ready / executing: the step is on its way.
    state = 'creating'
  }

  const source = sourceLabels(task)
  const sourceText = text(task.input?.text)
  const senderLabel =
    text(task.source_ref?.sender_display_name) || text(arguments_.source?.sender_display_name)

  return {
    taskId: task.task_id,
    taskStatus: task.status,
    state,
    title: text(arguments_.title) || '待办',
    timeLabel,
    timeExpression: timeExpression || undefined,
    dueAt: dueAt || undefined,
    startTime: startTime || undefined,
    endTime: endTime || undefined,
    timezone,
    notes: text(arguments_.notes) || undefined,
    senderLabel: senderLabel || undefined,
    sourceLabel: [source.label, text(conversationName) || source.conversation].filter(Boolean).join(' · '),
    sourceText,
    approvalId: approval?.approval_id,
    approvalVersion: approval?.version,
    arguments: { ...arguments_ },
    missingInformation,
    eventId: eventId || undefined,
    eventUrl: eventUrl || undefined,
    errorMessage: errorMessage || (cancelled ? '任务已取消' : undefined),
    verificationWarning:
      approval && approval.reconcilable === false
        ? '此操作提交后无法自动确认结果，需要你自行核对'
        : undefined,
  }
}

export function draftSortKey(draft: ScheduleDraft): number {
  const raw = draft.dueAt || draft.startTime
  const moment = raw ? new Date(raw).getTime() : Number.NaN
  return Number.isNaN(moment) ? Number.MAX_SAFE_INTEGER : moment
}

/** Offset of `zone` against UTC at `at`, in minutes east of UTC. */
function offsetMinutesIn(zone: string, at: Date): number {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).formatToParts(at)
    const read = (type: string) => Number(parts.find((part) => part.type === type)?.value || 0)
    const asUTC = Date.UTC(
      read('year'),
      read('month') - 1,
      read('day'),
      read('hour') % 24,
      read('minute'),
      read('second'),
    )
    return (asUTC - Math.floor(at.getTime() / 1000) * 1000) / 60000
  } catch {
    return 0
  }
}

/**
 * The last minute of `day` in `zone`, as an ISO instant.
 *
 * The owner edits a calendar day, not a clock time, so the deadline is the end
 * of that day — the same rule the backend uses for "明天" (23:59, not 00:00,
 * which would read as overdue the moment it was written).
 */
export function endOfDayISO(day: string, zone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day.trim())
  if (!match) return ''
  const [, year, month, date] = match
  const naiveUTC = Date.UTC(Number(year), Number(month) - 1, Number(date), 23, 59, 0)
  let instant = new Date(naiveUTC)
  for (let i = 0; i < 3; i += 1) {
    const offset = offsetMinutesIn(zone, instant)
    const next = new Date(naiveUTC - offset * 60_000)
    if (next.getTime() === instant.getTime()) break
    instant = next
  }
  return instant.toISOString()
}

/** The calendar day of an ISO instant, read in `zone`, as YYYY-MM-DD. */
export function dayOfISO(iso: string | undefined, zone: string): string {
  const raw = text(iso)
  if (!raw) return ''
  const moment = new Date(raw)
  if (Number.isNaN(moment.getTime())) return ''
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: zone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(moment)
    const read = (type: string) => parts.find((part) => part.type === type)?.value || ''
    const year = read('year')
    const month = read('month')
    const day = read('day')
    return year && month && day ? `${year}-${month}-${day}` : ''
  } catch {
    return raw.slice(0, 10)
  }
}

export interface DraftEdit {
  title: string
  /** YYYY-MM-DD, or '' when the owner cleared the time. */
  dueDate: string
}

export interface ComposedEdit {
  arguments: Record<string, unknown>
  /** Human-readable phrase to keep beside the resolved moment. */
  timeExpression: string
}

/**
 * Builds the argument payload for one edited draft.
 *
 * The backend re-parses `due_expression` with the *message's* timestamp
 * ("明天" means the day after the message was sent, which is the product rule),
 * so the edit sends the resolved `due_at` **and** clears `due_expression`.
 * A cleared date sends neither: the to-do keeps its title but loses its
 * deadline, which is a legitimate state.
 */
export function composeEditedArguments(draft: ScheduleDraft, edit: DraftEdit): ComposedEdit {
  const zone = draft.timezone || 'Asia/Shanghai'
  const dueDate = edit.dueDate.trim()
  const arguments_: Record<string, unknown> = {
    // Never keep the original hash-guarded values: every key the capability
    // accepts is sent, so an edit cannot leave a stale phrase behind.
    due_at: null,
    due_expression: null,
    timezone: zone,
  }
  let timeExpression = ''
  if (dueDate) {
    const dueAt = endOfDayISO(dueDate, zone)
    if (dueAt) {
      arguments_.due_at = dueAt
      arguments_.due_expression = null
    }
    timeExpression = dueDate
  }
  return { arguments: arguments_, timeExpression }
}

// The draft carries a conversation id, not a name. Resolve names from Knowledge
// with one refresh per minute so the card can show "飞书 · 研发组" instead of only
// the platform and the conversation type.
const conversationNameCache = new Map<string, string>()
const CONVERSATION_CACHE_MS = 60_000
let conversationCacheAt = 0

async function resolveConversationNames(tasks: AgentTask[]): Promise<Map<string, string>> {
  const ids = new Set<string>()
  const platforms = new Set<string>()
  for (const task of tasks) {
    const ref = task.source_ref || {}
    const id = text(ref.conversation_ingestion_id)
    const platform = text(ref.platform).toLowerCase()
    if (!id || !platform) continue
    ids.add(id)
    platforms.add(platform)
  }
  if (ids.size && Date.now() - conversationCacheAt > CONVERSATION_CACHE_MS) {
    for (const platform of platforms) {
      if (platform !== 'feishu' && platform !== 'wecom' && platform !== 'wechat') continue
      try {
        const conversations = await listConversations(platform as ConnectorPlatform)
        for (const conversation of conversations) {
          if (conversation?.id) conversationNameCache.set(conversation.id, text(conversation.name))
        }
      } catch {
        // Falls back to "platform · conversation type" on the card.
      }
    }
    conversationCacheAt = Date.now()
  }
  const resolved = new Map<string, string>()
  for (const id of ids) {
    const name = conversationNameCache.get(id)
    if (name) resolved.set(id, name)
  }
  return resolved
}

export interface LoadScheduleDraftsOptions {
  limit?: number
  /** Task ids whose confirmation is in flight, so the card shows "creating". */
  submittingTaskIDs?: string[]
  /**
   * Task ids that must stay on screen while the kernel works on them.
   *
   * A Task leaves the `waiting_approval`/`waiting_input` list the moment its step
   * starts, so a card that is not fetched by id would silently disappear right
   * after the user confirms it.
   */
  trackedTaskIDs?: string[]
}

/**
 * Ranks the approvals of one Task for the card.
 *
 * A pending decision always outranks an already accepted one: a re-plan starts a
 * new Step whose status is waiting_approval, so after user input both an
 * `approved` and a `waiting_approval` approval can carry version 1 and the fresh
 * request must win.
 *
 * An `expired` approval still ranks below a pending one but above an accepted
 * one: the preview it describes is unchanged (the Agent re-checks the argument
 * fingerprint before executing), so the card keeps a usable confirmation
 * instead of stranding the Task with no way to proceed.
 */
function approvalRank(approval: AgentApproval): number {
  const status = approval.status === 'waiting_approval' ? 1000 : approval.status === 'expired' ? 500 : 0
  return status + (approval.version || 0)
}

/**
 * Joins the three Agent endpoints into the drafts the sidebar renders.
 *
 * The approval carries the confirmed arguments, so the plan endpoint is only
 * needed when a draft has no approval yet. Observations are fetched only for
 * tasks that are waiting for input or already finished, because that is where
 * `missing_information` and the created event id live.
 */
export async function loadScheduleDrafts(options: LoadScheduleDraftsOptions = {}): Promise<ScheduleDraft[]> {
  const submitting = new Set(options.submittingTaskIDs || [])
  const [listed, approvals] = await Promise.all([listScheduleDrafts(options.limit ?? 20), listAgentApprovals()])
  const listedIDs = new Set(listed.map((item) => item.task_id))
  const missing = (options.trackedTaskIDs || []).filter((id) => id && !listedIDs.has(id))
  const tracked = await Promise.all(missing.map((id) => getAgentTask(id).catch(() => null)))
  const tasks = [...listed, ...tracked.filter((item): item is AgentTask => Boolean(item))]

  const approvalByTask = new Map<string, AgentApproval>()
  for (const approval of approvals) {
    if (
      approval.status !== 'waiting_approval' &&
      approval.status !== 'approved' &&
      approval.status !== 'expired'
    )
      continue
    const current = approvalByTask.get(approval.task_id)
    if (!current || approvalRank(approval) >= approvalRank(current)) {
      approvalByTask.set(approval.task_id, approval)
    }
  }
  const conversationNames = await resolveConversationNames(tasks)

  const built = await Promise.all(
    tasks.map(async (task) => {
      const approval = approvalByTask.get(task.task_id) || null
      const plan = approval ? null : await getAgentPlan(task.task_id).catch(() => null)
      const observations =
        task.status === 'waiting_approval'
          ? null
          : await listAgentObservations(task.task_id).catch(() => [] as AgentObservation[])
      const conversationID = text(task.source_ref?.conversation_ingestion_id)
      return buildScheduleDraft({
        task,
        approval,
        plan,
        observations,
        conversationName: conversationNames.get(conversationID),
        submitting: submitting.has(task.task_id),
      })
    }),
  )

  return built.sort((left, right) => draftSortKey(left) - draftSortKey(right))
}
