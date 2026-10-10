import { authenticatedFetch } from '../auth/request.ts'
import { getCurrentUser } from './core-auth.ts'
import { desktopApiBase } from './runtime-config.ts'

const env = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(desktopApiBase('/api/rag/api/v1') || env.VITE_RAG_BASE_URL || '/api/rag/api/v1').replace(/\/$/, '')
let userIDPromise: Promise<string> | null = null

function isUserID(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.trim())
}

async function currentUserID() {
  if (!userIDPromise) {
    userIDPromise = getCurrentUser().then((user) => {
      const userID = String(user?.id || '').trim()
      if (!isUserID(userID)) throw new Error('authenticated user identity is unavailable')
      return userID
    }).catch((error) => {
      // A failed identity lookup must never poison the cache or turn into a
      // search request made as a different user.
      userIDPromise = null
      throw error
    })
  }
  return userIDPromise
}

async function headers(accept = 'application/json') {
  const value = new Headers({ Accept: accept, 'Content-Type': 'application/json' })
  const userID = await currentUserID()
  if (userID) value.set('X-User-ID', userID)
  return value
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await authenticatedFetch(`${baseURL}${path}`, { ...init, headers: await headers() })
  const raw = await response.text()
  let body: any = null
  try { body = raw ? JSON.parse(raw) : null } catch { body = raw }
  if (!response.ok) throw new Error(response.status === 401 ? '登录已过期，请重新登录' : body?.detail || body?.message || `RAG request failed (${response.status})`)
  return body as T
}

export type QaCitation = Record<string, any>
export type QaMessage = { id: string; role: 'user' | 'assistant' | 'system'; content: string; citations?: QaCitation[]; status: string; created_at?: string | null }
export type QaConversation = { id: string; title: string; message_count: number; updated_at?: string | null; last_message_at?: string | null; messages?: QaMessage[] }

export type RagSearchItem = Record<string, any>
export type RagSearchResponse = {
  request_id?: string
  query?: string
  items: RagSearchItem[]
  diagnostics?: Record<string, any>
}

export type RagSearchInput = {
  query: string
  organizationId?: string
  knowledgeBaseIds?: string[]
  topK?: number
  includeProtected?: boolean
  signal?: AbortSignal
  /**
   * Which partition to search. Personal libraries (私人私聊 / 本地知识库) live
   * in the user scope; the RAG API defaults to organization scope, so a
   * personal library that forgot to send this could never match its own files.
   */
  scopeType?: 'organization' | 'user'
}

function cleanKnowledgeBaseIds(values?: string[], single?: string) {
  return [...new Set([...(values || []), ...(single ? [single] : [])].map((value) => String(value || '').trim()).filter(Boolean))]
}

export function listQaConversations(page = 1, pageSize = 20) { return request<{ items: QaConversation[]; page: number; page_size: number; total: number }>(`/qa/conversations?page=${page}&page_size=${pageSize}`) }
export function getQaConversation(id: string) { return request<QaConversation>(`/qa/conversations/${encodeURIComponent(id)}`) }
export function createQaConversation(title = '新的对话') { return request<{ id: string; title: string }>(`/qa/conversations`, { method: 'POST', body: JSON.stringify({ title }) }) }
export function renameQaConversation(id: string, title: string) { return request<{ id: string; title: string }>(`/qa/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ title }) }) }
export function deleteQaConversation(id: string) { return request<{ status: string }>(`/qa/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }) }

export function searchGlobal(input: RagSearchInput) {
  const knowledgeBaseIds = cleanKnowledgeBaseIds(input.knowledgeBaseIds)
  return request<RagSearchResponse>('/search/global', {
    method: 'POST',
    signal: input.signal,
    body: JSON.stringify({
      query: input.query,
      scope_type: input.scopeType || 'organization',
      organization_id: input.organizationId || undefined,
      knowledge_base_ids: knowledgeBaseIds.length ? knowledgeBaseIds : undefined,
      top_k: input.topK ?? 12,
      include_protected: input.includeProtected ?? true,
    }),
  })
}

export function searchKeyword(input: RagSearchInput) {
  const knowledgeBaseIds = cleanKnowledgeBaseIds(input.knowledgeBaseIds)
  return request<RagSearchResponse>('/search/keyword', {
    method: 'POST',
    signal: input.signal,
    body: JSON.stringify({
      query: input.query,
      scope_type: input.scopeType || 'organization',
      organization_id: input.organizationId || undefined,
      knowledge_base_ids: knowledgeBaseIds.length ? knowledgeBaseIds : undefined,
      top_k: input.topK ?? 20,
      include_protected: input.includeProtected ?? true,
    }),
  })
}

export function searchKnowledge(input: RagSearchInput & { knowledgeBaseId?: string; knowledgeBaseIds?: string[] }) {
  const knowledgeBaseIds = cleanKnowledgeBaseIds(input.knowledgeBaseIds, input.knowledgeBaseId)
  if (!knowledgeBaseIds.length) throw new Error('knowledge_base_id is required')
  return request<RagSearchResponse>('/search/knowledge', {
    method: 'POST',
    signal: input.signal,
    body: JSON.stringify({
      query: input.query,
      scope_type: input.scopeType || 'organization',
      knowledge_base_id: knowledgeBaseIds.length === 1 ? knowledgeBaseIds[0] : undefined,
      knowledge_base_ids: knowledgeBaseIds,
      organization_id: input.organizationId || undefined,
      top_k: input.topK ?? 20,
      include_protected: input.includeProtected ?? true,
    }),
  })
}

export async function askQaStream(input: { query: string; conversationId?: string | number; mode?: 'quick' | 'deep'; knowledgeBaseIds?: string[]; organizationId?: string }, handlers: { onMeta?: (value: any) => void; onToken?: (value: string) => void; onCitation?: (value: any) => void; onDone?: (value: any) => void; onError?: (value: any) => void }) {
  const response = await authenticatedFetch(`${baseURL}/ai/documents/stream`, { method: 'POST', headers: await headers('text/event-stream'), body: JSON.stringify({ query: input.query, conversation_id: input.conversationId ? String(input.conversationId) : undefined, mode: input.mode || 'quick', knowledge_base_ids: input.knowledgeBaseIds || [], organization_id: input.organizationId }) })
  if (!response.ok || !response.body) throw new Error(response.status === 401 ? '登录已过期，请重新登录' : `RAG stream failed (${response.status})`)
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
  const dispatch = (raw: string) => {
    const lines = raw.split(/\r?\n/); const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message'; const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
    let value: any = {}; try { value = data ? JSON.parse(data) : {} } catch { return }
    if (event === 'meta') handlers.onMeta?.(value)
    else if (event === 'token') handlers.onToken?.(String(value.delta || ''))
    else if (event === 'citation') handlers.onCitation?.(value.citation)
    else if (event === 'done') handlers.onDone?.(value)
    else if (event === 'error') handlers.onError?.(value)
  }
  while (true) { const chunk = await reader.read(); if (chunk.done) break; buffer += decoder.decode(chunk.value, { stream: true }); const parts = buffer.split(/\r?\n\r?\n/); buffer = parts.pop() || ''; for (const part of parts) dispatch(part) }
  if (buffer.trim()) dispatch(buffer)
}

// --- 候选实体审核（管理端） -------------------------------------------------
// 这些接口要求组织级 entity:review 能力，服务端会调用 Core 做校验，因此必须
// 同时带上 X-User-ID 与 X-Organization-Id。

export type EntityCandidate = {
  candidate_id: string
  candidate_name: string
  candidate_domain: string
  normalized_key: string
  score: number
  status: string
  mention_count: number
  distinct_chunk_count: number
  distinct_source_count: number
  distinct_conversation_count: number
  suggested_entity_id?: string | null
  resolved_entity_id?: string | null
  first_seen_at?: string | null
  last_seen_at?: string | null
}

export type EntityCandidateMention = {
  mention_id: string
  chunk_id: string
  surface_form: string
  candidate_domain?: string
  context_excerpt?: string | null
  confidence: number
  extraction_method: string
  created_at?: string | null
}

export type EntityCandidateDetail = EntityCandidate & {
  review_note?: string | null
  mentions: EntityCandidateMention[]
}

export type EntityReviewAction = 'promote' | 'merge' | 'ignore' | 'defer'

export type EntityReviewResult = {
  candidate_id: string
  status: string
  resolved_entity_id?: string | null
  registry_version?: number | null
  branch_refresh_job_id?: string | null
}

async function adminHeaders(organizationId: string) {
  const value = await headers()
  const scope = String(organizationId || '').trim()
  if (scope) value.set('X-Organization-Id', scope)
  return value
}

async function adminRequest<T>(path: string, organizationId: string, init: RequestInit = {}): Promise<T> {
  const response = await authenticatedFetch(`${baseURL}${path}`, { ...init, headers: await adminHeaders(organizationId) })
  const raw = await response.text()
  let body: any = null
  try { body = raw ? JSON.parse(raw) : null } catch { body = raw }
  if (!response.ok) {
    if (response.status === 403) throw new Error('没有实体审核权限，请联系组织管理员')
    throw new Error(body?.detail || body?.message || `RAG admin request failed (${response.status})`)
  }
  return body as T
}

export function listEntityCandidates(input: {
  organizationId: string
  status?: string
  domain?: string
  query?: string
  minScore?: number
  page?: number
  pageSize?: number
}) {
  const params = new URLSearchParams()
  params.set('scope_type', 'organization')
  params.set('page', String(input.page ?? 1))
  params.set('page_size', String(input.pageSize ?? 20))
  if (input.status) params.set('status', input.status)
  if (input.domain) params.set('domain', input.domain)
  if (input.query) params.set('query', input.query)
  if (input.minScore) params.set('min_score', String(input.minScore))
  return adminRequest<{ items: EntityCandidate[]; page: number; page_size: number; total: number }>(
    `/admin/entity-candidates?${params.toString()}`, input.organizationId,
  )
}

export function getEntityCandidate(candidateId: string, organizationId: string) {
  return adminRequest<EntityCandidateDetail>(
    `/admin/entity-candidates/${encodeURIComponent(candidateId)}?scope_type=organization`,
    organizationId,
  )
}

export type AdminEntity = {
  entity_id: string
  domain: string
  canonical_name: string
  normalized_key: string
  status: string
  registry_version: number
}

export function listAdminEntities(organizationId: string) {
  return adminRequest<{ items: AdminEntity[] }>(
    '/admin/entities?scope_type=organization', organizationId,
  )
}

export function reviewEntityCandidate(input: {
  candidateId: string
  organizationId: string
  action: EntityReviewAction
  reviewRequestId: string
  canonicalName?: string
  domain?: string
  targetEntityId?: string
  note?: string
  expectedStatus?: string
  /** 审核停留时长（毫秒）；批量审核没有停留过程，不传。 */
  durationMs?: number
}) {
  return adminRequest<EntityReviewResult>(
    `/admin/entity-candidates/${encodeURIComponent(input.candidateId)}/review?scope_type=organization`,
    input.organizationId,
    {
      method: 'POST',
      // review_request_id 是幂等键：重试同一次审核不会重复创建实体。
      headers: { 'Idempotency-Key': input.reviewRequestId },
      body: JSON.stringify({
        review_request_id: input.reviewRequestId,
        action: input.action,
        canonical_name: input.canonicalName || undefined,
        domain: input.domain || undefined,
        target_entity_id: input.targetEntityId || undefined,
        note: input.note || undefined,
        expected_status: input.expectedStatus || undefined,
        duration_ms: input.durationMs ?? undefined,
      }),
    },
  )
}
