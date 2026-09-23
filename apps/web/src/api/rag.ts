import { getAccessToken, getCurrentUser } from './core-auth'

const env = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(env.VITE_RAG_BASE_URL || '/api/rag/api/v1').replace(/\/$/, '')
let userIDPromise: Promise<string> | null = null

async function currentUserID() {
  const configured = String(env.VITE_RAG_USER_ID || env.VITE_KNOWLEDGE_DEV_USER_ID || '').trim()
  if (configured) return configured
  // RAG's QA tables store user_id as UUID. Keep an explicit UUID fallback for
  // local development when Core authentication is not running.
  if (!userIDPromise) userIDPromise = getCurrentUser().then((user) => user.id).catch(() => '00000000-0000-0000-0000-000000000001')
  return userIDPromise
}

async function headers(accept = 'application/json') {
  const value = new Headers({ Accept: accept, 'Content-Type': 'application/json' })
  const token = getAccessToken()
  if (token) value.set('Authorization', `Bearer ${token}`)
  const userID = await currentUserID()
  if (userID) value.set('X-User-ID', userID)
  return value
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${baseURL}${path}`, { ...init, headers: await headers() })
  const raw = await response.text()
  let body: any = null
  try { body = raw ? JSON.parse(raw) : null } catch { body = raw }
  if (!response.ok) throw new Error(body?.detail || body?.message || `RAG request failed (${response.status})`)
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
      organization_id: input.organizationId || undefined,
      knowledge_base_ids: knowledgeBaseIds.length ? knowledgeBaseIds : undefined,
      top_k: input.topK ?? 12,
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
      knowledge_base_id: knowledgeBaseIds.length === 1 ? knowledgeBaseIds[0] : undefined,
      knowledge_base_ids: knowledgeBaseIds,
      organization_id: input.organizationId || undefined,
      top_k: input.topK ?? 20,
      include_protected: input.includeProtected ?? true,
    }),
  })
}

export async function askQaStream(input: { query: string; conversationId?: string | number; mode?: 'quick' | 'deep'; knowledgeBaseIds?: string[]; organizationId?: string }, handlers: { onMeta?: (value: any) => void; onToken?: (value: string) => void; onCitation?: (value: any) => void; onDone?: (value: any) => void; onError?: (value: any) => void }) {
  const response = await fetch(`${baseURL}/ai/documents/stream`, { method: 'POST', headers: await headers('text/event-stream'), body: JSON.stringify({ query: input.query, conversation_id: input.conversationId ? String(input.conversationId) : undefined, mode: input.mode || 'quick', knowledge_base_ids: input.knowledgeBaseIds || [], organization_id: input.organizationId }) })
  if (!response.ok || !response.body) throw new Error(`RAG stream failed (${response.status})`)
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
