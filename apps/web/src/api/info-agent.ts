import { getAccessToken, getCurrentUser } from './core-auth.ts'
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

// The Agent API authorises every call by owner, and that owner must be the Core
// user uuid (the same value Knowledge maps external identities to). A bogus or
// placeholder identity must fail closed instead of returning someone else's
// drafts.
let userIDPromise: Promise<string> | null = null
let cachedToken = ''

function isUserID(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.trim())
}

export async function currentAgentUserID(): Promise<string> {
  // Re-resolve when the session token changes: a different token can be a
  // different user, and the drafts of the previous user must never leak through.
  const token = getAccessToken()
  if (!userIDPromise || cachedToken !== token) {
    cachedToken = token
    userIDPromise = getCurrentUser()
      .then((user) => {
        const userID = String(user?.id || '').trim()
        if (!isUserID(userID)) throw new AgentApiError('authenticated user identity is unavailable', 'identity_unavailable', 401, false)
        return userID
      })
      .catch((error) => {
        userIDPromise = null
        throw error
      })
  }
  return userIDPromise
}

async function agentHeaders(): Promise<Headers> {
  const headers = new Headers({ Accept: 'application/json', 'Content-Type': 'application/json' })
  const token = getAccessToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  headers.set('X-Agent-User-Id', await currentAgentUserID())
  return headers
}

async function agentRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${baseURL}${path}`, { ...init, headers: await agentHeaders() })
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
      body?.message || body?.detail || `Agent request failed (${response.status})`,
      body?.code || 'request_failed',
      response.status,
      Boolean(body?.retryable),
    )
  }
  return body as T
}

export interface AgentTask {
  task_id: string
  source_type: string
  owner_user_id: string
  status: string
  input?: Record<string, any>
  source_ref?: Record<string, any>
  objective?: string | null
  last_error?: Record<string, any> | null
  created_at?: string
  updated_at?: string
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

export async function approveAgentApproval(approvalID: string, version: number): Promise<AgentApproval> {
  return agentRequest<AgentApproval>(`/approvals/${encodeURIComponent(approvalID)}/approve`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export type ScheduleDraftState =
  | 'confirmable'
  | 'needs_calendar'
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
  timeExpression?: string
  startTime?: string
  endTime?: string
  timezone?: string
  location?: string
  description?: string
  sourceLabel: string
  sourceText: string
  approvalId?: string
  approvalVersion?: number
  missingInformation: string[]
  eventId?: string
  eventUrl?: string
  errorMessage?: string
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

function sourceLabels(task: AgentTask): { label: string; conversation: string } {
  const ref = task.source_ref || {}
  const platform = text(ref.platform).toLowerCase()
  const conversationType = text(ref.conversation_type).toLowerCase()
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
  const startTime = text(arguments_.start_time)
  const endTime = text(arguments_.end_time)
  const timeExpression = text(arguments_.time_expression)
  const startLabel = formatMoment(startTime, timezone)
  const endLabel = formatMoment(endTime, timezone)

  let timeLabel = ''
  if (startLabel) timeLabel = endLabel ? `${startLabel} → ${endLabel}` : startLabel
  else if (timeExpression) timeLabel = timeExpression
  else timeLabel = '时间待补充'

  const observation = latestObservation(observations || [])
  const missingInformation = Array.isArray(observation?.output?.missing_information)
    ? (observation?.output?.missing_information as unknown[]).map((item) => String(item))
    : []
  const eventId = text(observation?.output?.event_id)
  const eventUrl = text(observation?.output?.event_url)
  const observationError = observation?.status && observation.status !== 'succeeded' ? observation.error : null
  const errorMessage =
    text(observationError?.message) ||
    text(task.last_error?.message) ||
    (observation?.status === 'unknown' ? '外部结果未知，请联系管理员确认是否已创建' : '')

  let state: ScheduleDraftState = 'needs_input'
  if (task.status === 'waiting_approval') state = 'confirmable'
  else if (task.status === 'waiting_input') {
    state = missingInformation.includes('calendar_authorization') ? 'needs_calendar' : 'needs_input'
  } else if (task.status === 'succeeded') state = 'created'
  else if (task.status === 'failed' || task.status === 'unknown') state = 'failed'

  if (submitting && task.status !== 'succeeded' && task.status !== 'failed' && task.status !== 'unknown') {
    state = 'creating'
  }
  if (task.status === 'succeeded') state = 'created'

  const source = sourceLabels(task)
  const sourceText = text(task.input?.text)

  return {
    taskId: task.task_id,
    taskStatus: task.status,
    state,
    title: text(arguments_.title) || '日程',
    timeLabel,
    timeExpression: timeExpression || undefined,
    startTime: startTime || undefined,
    endTime: endTime || undefined,
    timezone,
    location: text(arguments_.location) || undefined,
    description: text(arguments_.description) || undefined,
    sourceLabel: [source.label, text(conversationName) || source.conversation].filter(Boolean).join(' · '),
    sourceText,
    approvalId: approval?.approval_id,
    approvalVersion: approval?.version,
    missingInformation,
    eventId: eventId || undefined,
    eventUrl: eventUrl || undefined,
    errorMessage: errorMessage || undefined,
  }
}

export function draftSortKey(draft: ScheduleDraft): number {
  const moment = draft.startTime ? new Date(draft.startTime).getTime() : Number.NaN
  return Number.isNaN(moment) ? Number.MAX_SAFE_INTEGER : moment
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
  const [tasks, approvals] = await Promise.all([listScheduleDrafts(options.limit ?? 20), listAgentApprovals()])

  const approvalByTask = new Map<string, AgentApproval>()
  for (const approval of approvals) {
    if (approval.status !== 'waiting_approval' && approval.status !== 'approved') continue
    const current = approvalByTask.get(approval.task_id)
    if (!current || (approval.version || 0) >= (current.version || 0)) approvalByTask.set(approval.task_id, approval)
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
