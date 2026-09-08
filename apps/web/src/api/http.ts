export class ApiError extends Error {
  code: string
  status: number
  retryable: boolean

  constructor(message: string, code = 'request_failed', status = 500, retryable = false) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.retryable = retryable
  }
}

const appEnv = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(appEnv.VITE_KNOWLEDGE_BASE_URL || '/api/knowledge/v1').replace(/\/$/, '')

function accessToken() {
  try {
    const session = typeof sessionStorage === 'undefined' ? '' : sessionStorage.getItem('access_token') || ''
    const local = typeof localStorage === 'undefined' ? '' : localStorage.getItem('access_token') || ''
    return session || local
  } catch {
    return ''
  }
}

export function knowledgeHeaders(initial?: HeadersInit, accept = 'application/json') {
  const headers = new Headers(initial)
  headers.set('Accept', accept)
  if (!headers.has('X-Request-ID')) headers.set('X-Request-ID', requestIdentifier())
  if (!headers.has('X-Trace-ID')) headers.set('X-Trace-ID', requestIdentifier())
  const token = accessToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  else {
    headers.set('X-User-ID', appEnv.VITE_KNOWLEDGE_DEV_USER_ID || 'dev-user')
    headers.set('X-Organization-ID', appEnv.VITE_KNOWLEDGE_DEV_ORGANIZATION_ID || 'dev-org')
  }
  return headers
}

function requestIdentifier() {
  try {
    if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  } catch {
    // Fall through to a best-effort identifier for older browsers.
  }
  return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export async function knowledgeRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = knowledgeHeaders(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${baseURL}${path.startsWith('/') ? path : `/${path}`}`, { ...init, headers })
  const raw = await response.text()
  let body: any = null
  if (raw) {
    try { body = JSON.parse(raw) } catch { body = raw }
  }
  if (!response.ok) {
    const error = body && typeof body === 'object' ? body : {}
    throw new ApiError(error.message || `Knowledge request failed (${response.status})`, error.code || 'request_failed', response.status, Boolean(error.retryable))
  }
  return body as T
}

export function knowledgeContentURL(path: string) {
  return `${baseURL}${path.startsWith('/') ? path : `/${path}`}`
}
