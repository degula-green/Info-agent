import { authenticatedFetch } from '../auth/request.ts'
import { resolveOrganizationId } from '@/utils/info-search-scope'

export class ApiError extends Error {
  code: string
  status: number
  retryable: boolean

  constructor(message: string, code = 'request_failed', status = 500, retryable = false) {
    super(code === 'AUTH_UNAUTHENTICATED' || status === 401 ? '登录已过期，请重新登录' : message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.retryable = retryable
  }
}

const appEnv = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(appEnv.VITE_KNOWLEDGE_BASE_URL || '/api/knowledge/v1').replace(/\/$/, '')

// Keep browser traffic bounded while the collectors are catching up. The
// Knowledge service is intentionally polled conservatively so a slow detail
// request cannot create an unbounded backlog of pending fetches.
const requestConcurrency = 2
const requestSpacingMs = 40
type RequestTask<T> = { run: () => Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void }
const requestQueue: RequestTask<unknown>[] = []
let activeRequests = 0
let nextRequestAt = 0

function drainRequestQueue() {
  while (activeRequests < requestConcurrency && requestQueue.length) {
    const task = requestQueue.shift()!
    const now = Date.now()
    const startAt = Math.max(now, nextRequestAt)
    nextRequestAt = startAt + requestSpacingMs
    const start = () => {
      Promise.resolve().then(task.run).then(task.resolve, task.reject).finally(() => {
        activeRequests -= 1
        drainRequestQueue()
      })
    }
    // Reserve the slot before scheduling so a burst cannot enqueue more than
    // the configured number of delayed requests.
    activeRequests += 1
    if (startAt > now) globalThis.setTimeout(start, startAt - now)
    else start()
  }
}

function enqueueRequest<T>(run: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    requestQueue.push({ run, resolve: resolve as (value: unknown) => void, reject })
    drainRequestQueue()
  })
}

let organizationHeaderID: string | undefined
let organizationHeaderPromise: Promise<string | undefined> | null = null
function organizationHeader() {
  if (organizationHeaderID) return Promise.resolve(organizationHeaderID)
  if (!organizationHeaderPromise) {
    organizationHeaderPromise = resolveOrganizationId().then((id) => {
      organizationHeaderID = id
      return id
    }).catch(() => undefined).finally(() => { organizationHeaderPromise = null })
  }
  return organizationHeaderPromise
}
export function knowledgeHeaders(initial?: HeadersInit, accept = 'application/json') {
  const headers = new Headers(initial)
  headers.set('Accept', accept)
  if (!headers.has('X-Request-ID')) headers.set('X-Request-ID', requestIdentifier())
  if (!headers.has('X-Trace-ID')) headers.set('X-Trace-ID', requestIdentifier())
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

async function performKnowledgeRequest<T>(path: string, init: RequestInit): Promise<T> {
  const headers = knowledgeHeaders(init.headers)
  const organizationID = await organizationHeader()
  if (organizationID && !headers.has('X-Organization-ID')) headers.set('X-Organization-ID', organizationID)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await enqueueRequest(() => authenticatedFetch(`${baseURL}${path.startsWith('/') ? path : `/${path}`}`, { ...init, headers }))
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

const inflightGets = new Map<string, Promise<unknown>>()

export function knowledgeRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = String(init.method || 'GET').toUpperCase()
  if (method !== 'GET' || init.body) return performKnowledgeRequest<T>(path, init)
  const key = `${method} ${path}`
  const existing = inflightGets.get(key)
  if (existing) return existing as Promise<T>
  const request = performKnowledgeRequest<T>(path, init)
  inflightGets.set(key, request)
  const clear = () => {
    if (inflightGets.get(key) === request) inflightGets.delete(key)
  }
  void request.then(clear, clear)
  return request
}

export function knowledgeContentURL(path: string) {
  return `${baseURL}${path.startsWith('/') ? path : `/${path}`}`
}

export function knowledgeFetch(url: string, init: RequestInit = {}) {
  return enqueueRequest(() => authenticatedFetch(url, init))
}
