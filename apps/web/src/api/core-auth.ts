export interface CoreUser { id: string; email: string; nickname: string; status: string; avatar_url?: string }
export interface CoreTokenResponse { access_token: string; token_type: string; expires_at: string }
export class CoreAuthError extends Error {
  code: string
  status: number
  retryable: boolean
  constructor(message: string, code = 'request_failed', status = 500, retryable = false) {
    super(message)
    this.name = 'CoreAuthError'
    this.code = code
    this.status = status
    this.retryable = retryable
  }
}
const env = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(env.VITE_CORE_BASE_URL || '/api/core').replace(/\/$/, '')
let refreshPromise: Promise<CoreTokenResponse> | null = null

// Refresh tokens are rotated on every successful refresh. A process-local
// promise protects one tab, while this coordinator extends the same
// single-flight guarantee to every tab in the browser profile.
const refreshChannelName = 'info-agent:core-auth-refresh'
const refreshLockKey = 'info-agent:core-auth-refresh-lock'
const refreshResultKey = 'info-agent:core-auth-refresh-result'
const refreshLockTTL = 10_000
const refreshWaitTTL = 12_000
let refreshChannel: BroadcastChannel | null = null
let refreshTabID = ''

type RefreshMessage = { type: 'success' | 'failure'; owner: string; result?: CoreTokenResponse; error?: { message: string; code?: string; status?: number } }
type RefreshEnvelope = RefreshMessage & { at?: number }

function tabID() {
  if (refreshTabID) return refreshTabID
  refreshTabID = requestID()
  return refreshTabID
}
function readStorage(key: string) { try { return typeof localStorage === 'undefined' ? null : localStorage.getItem(key) } catch { return null } }
function writeStorage(key: string, value: string) { try { if (typeof localStorage !== 'undefined') localStorage.setItem(key, value); return true } catch { return false } }
function removeStorage(key: string) { try { if (typeof localStorage !== 'undefined') localStorage.removeItem(key) } catch { /* unavailable */ } }
function getRefreshChannel() {
  if (refreshChannel || typeof BroadcastChannel === 'undefined') return refreshChannel
  try { refreshChannel = new BroadcastChannel(refreshChannelName) } catch { refreshChannel = null }
  return refreshChannel
}
function publishRefresh(message: RefreshMessage) {
  try { getRefreshChannel()?.postMessage(message) } catch { /* unavailable */ }
  // localStorage is only a cross-tab signal. Never duplicate the access token
  // in the coordination record; saveAccessToken already updates the shared
  // access-token slot used by the waiting tab.
  const signal: RefreshEnvelope = { type: message.type, owner: message.owner, at: Date.now(), error: message.error }
  writeStorage(refreshResultKey, JSON.stringify(signal))
}
function sharedRefreshResult(message: RefreshEnvelope): CoreTokenResponse | null {
  if (message.type === 'failure') {
    throw refreshError(message.error?.message || 'authentication required', message.error?.code, message.error?.status)
  }
  if (message.result?.access_token) return message.result
  const accessToken = getAccessToken()
  if (!accessToken) return null
  return { access_token: accessToken, token_type: 'Bearer', expires_at: '' }
}
function lockOwner() { return tabID() }
function acquireRefreshLock(owner: string) {
  const now = Date.now()
  const raw = readStorage(refreshLockKey)
  if (raw) { try { const lock = JSON.parse(raw); if (lock.expiresAt > now && lock.owner !== owner) return false } catch { /* replace malformed lock */ } }
  if (!writeStorage(refreshLockKey, JSON.stringify({ owner, expiresAt: now + refreshLockTTL }))) return false
  const confirmed = readStorage(refreshLockKey)
  try { return Boolean(confirmed && JSON.parse(confirmed).owner === owner) } catch { return false }
}
function releaseRefreshLock(owner: string) {
  const raw = readStorage(refreshLockKey)
  try { if (raw && JSON.parse(raw).owner === owner) removeStorage(refreshLockKey) } catch { /* ignore malformed lock */ }
}
function refreshError(message: string, code = 'request_failed', status = 401) { return new CoreAuthError(message, code, status, false) }
function parseFreshRefreshResult(raw: string): RefreshEnvelope | null {
  try {
    const message = JSON.parse(raw) as RefreshEnvelope
    if (message.at && Date.now() - message.at > refreshWaitTTL) return null
    return message
  } catch { return null }
}

function requestID() { return globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}` }
export function getAccessToken() { try { return localStorage.getItem('access_token') || sessionStorage.getItem('access_token') || '' } catch { return '' } }
export function saveAccessToken(token: string) { try { sessionStorage.setItem('access_token', token); localStorage.setItem('access_token', token) } catch { /* ignore unavailable storage */ } }
async function request<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const headers = new Headers(init.headers); headers.set('Accept', 'application/json'); headers.set('Content-Type', 'application/json'); headers.set('X-Request-ID', requestID()); headers.set('X-Trace-ID', requestID())
  // Core's authenticated endpoints use the access token persisted after login.
  // Keep this in the shared request path so /auth/me and all subsequent Core
  // calls use the same authentication behavior.
  try {
    const token = getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
  } catch {
    // Storage may be unavailable in non-browser/test environments.
  }
  const response = await fetch(`${baseURL}${path}`, { ...init, credentials: 'include', headers })
  const raw = await response.text(); let body: any = null; if (raw) { try { body = JSON.parse(raw) } catch { body = raw } }
  if (response.status === 401 && !retried && path !== '/auth/refresh' && path !== '/auth/login' && path !== '/auth/register') { try { const token = await refresh(); if (token?.access_token) return request<T>(path, init, true) } catch { /* fall through with the original auth error */ } }
  if (!response.ok) { const e = body && typeof body === 'object' ? body : {}; throw new CoreAuthError(e.message || `Core request failed (${response.status})`, e.code || 'request_failed', response.status, Boolean(e.retryable)) }
  return body as T
}
export const login = (email: string, password: string) => request<CoreTokenResponse>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })
export const register = (email: string, username: string, password: string, confirm: string) => request<CoreUser>('/auth/register', { method: 'POST', body: JSON.stringify({ email, username, password, confirm }) })
async function waitForPeerRefresh(owner: string): Promise<CoreTokenResponse> {
  const startedAt = Date.now()
  while (Date.now() - startedAt < refreshWaitTTL) {
    const raw = readStorage(refreshResultKey)
    if (raw) { try { const message = parseFreshRefreshResult(raw); if (!message || message.owner === owner) { continue } const result = sharedRefreshResult(message); if (result) { saveAccessToken(result.access_token); return result } } catch (error) { if (error instanceof CoreAuthError) throw error } }
    await new Promise<void>((resolve) => setTimeout(resolve, 50))
  }
  throw refreshError('authentication required', 'AUTH_UNAUTHENTICATED', 401)
}
async function performOwnedRefresh(owner: string): Promise<CoreTokenResponse> {
  try {
    const result = await request<CoreTokenResponse>('/auth/refresh', { method: 'POST' })
    saveAccessToken(result.access_token)
    publishRefresh({ type: 'success', owner, result })
    return result
  } catch (error) {
    const e = error instanceof CoreAuthError ? error : refreshError('authentication required')
    publishRefresh({ type: 'failure', owner, error: { message: e.message, code: e.code, status: e.status } })
    throw e
  }
}

async function refreshWithCrossTabCoordination(): Promise<CoreTokenResponse> {
  // SSR, tests, and embedded non-browser consumers have no shared storage;
  // the process-local promise still protects those callers.
  if (typeof window === 'undefined' || typeof localStorage === 'undefined' || typeof navigator === 'undefined') {
    const result = await request<CoreTokenResponse>('/auth/refresh', { method: 'POST' })
    saveAccessToken(result.access_token)
    return result
  }
  const owner = lockOwner()
  const startedAt = Date.now()
  const locks = (navigator as Navigator & { locks?: { request<T>(name: string, options: { ifAvailable: boolean }, callback: (lock: unknown) => Promise<T>): Promise<T> } }).locks
  // Web Locks is an atomic cross-tab mutex. Use the storage lock only for
  // older browsers that do not expose it.
  if (locks) {
    return locks.request(refreshLockKey, { ifAvailable: true }, async (lock) => {
      if (!lock) return waitForPeerRefresh(owner)
      return performOwnedRefresh(owner)
    })
  }
  let lastSeenResult = ''
  const onMessage = (event: MessageEvent<RefreshMessage>) => {
    const message = event.data
    if (!message || message.owner === owner || (message.type !== 'success' && message.type !== 'failure')) return
    lastSeenResult = JSON.stringify({ ...message, at: Date.now() })
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key === refreshResultKey && event.newValue) lastSeenResult = event.newValue
  }
  getRefreshChannel()?.addEventListener('message', onMessage)
  window.addEventListener('storage', onStorage)
  try {
    while (Date.now() - startedAt < refreshWaitTTL) {
      if (acquireRefreshLock(owner)) {
        try {
          // Re-check the result published by another tab immediately before
          // taking the network path; this closes the lock hand-off race.
          if (lastSeenResult) {
            const message = parseFreshRefreshResult(lastSeenResult)
            if (message && message.owner !== owner) {
              const result = sharedRefreshResult(message)
              if (result) { saveAccessToken(result.access_token); return result }
            }
          }
          const result = await request<CoreTokenResponse>('/auth/refresh', { method: 'POST' })
          saveAccessToken(result.access_token)
          publishRefresh({ type: 'success', owner, result })
          return result
        } catch (error) {
          const e = error instanceof CoreAuthError ? error : refreshError('authentication required')
          publishRefresh({ type: 'failure', owner, error: { message: e.message, code: e.code, status: e.status } })
          throw e
        } finally { releaseRefreshLock(owner) }
      }
      if (lastSeenResult) {
        const message = parseFreshRefreshResult(lastSeenResult)
        if (!message) { lastSeenResult = '' }
        else {
          const result = sharedRefreshResult(message)
          if (result) { saveAccessToken(result.access_token); return result }
        }
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 50))
    }
    throw refreshError('authentication required', 'AUTH_UNAUTHENTICATED', 401)
  } finally {
    getRefreshChannel()?.removeEventListener('message', onMessage)
    window.removeEventListener('storage', onStorage)
  }
}

export async function refresh() {
  if (refreshPromise) return refreshPromise
  refreshPromise = refreshWithCrossTabCoordination().finally(() => { refreshPromise = null })
  return refreshPromise
}
export const logout = () => request<void>('/auth/logout', { method: 'POST' })
export const getCurrentUser = () => request<CoreUser>('/auth/me', { method: 'GET' })
export const updateCurrentUser = (nickname: string) => request<CoreUser>('/auth/me', { method: 'PATCH', body: JSON.stringify({ nickname }) })
export async function uploadAvatar(file: File, retried = false, accessToken?: string): Promise<CoreUser> { const headers = new Headers({ Accept: 'application/json', 'X-Request-ID': requestID(), 'X-Trace-ID': requestID() }); const token = accessToken || getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`); const data = new FormData(); data.append('file', file); const response = await fetch(`${baseURL}/auth/me/avatar`, { method: 'POST', body: data, credentials: 'include', headers }); const raw = await response.text(); let body: any = null; try { body = raw ? JSON.parse(raw) : null } catch { throw new CoreAuthError('头像服务返回了无效响应', 'AUTH_INVALID_RESPONSE', response.status || 502) } if (response.status === 401 && !retried) { try { const refreshed = await refresh(); return uploadAvatar(file, true, refreshed.access_token) } catch { /* fall through */ } } if (!response.ok) throw new CoreAuthError(body?.message || 'avatar upload failed', body?.code || 'request_failed', response.status); return body as CoreUser }
export async function downloadAvatar(retried = false): Promise<string> { const headers = new Headers({ Accept: 'image/*', 'X-Request-ID': requestID(), 'X-Trace-ID': requestID() }); const token = getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`); const response = await fetch(`${baseURL}/auth/me/avatar`, { credentials: 'include', headers }); if (response.status === 401 && !retried) { try { await refresh(); return downloadAvatar(true) } catch { /* fall through */ } } if (!response.ok) throw new CoreAuthError('avatar download failed', 'AVATAR_NOT_FOUND', response.status); return URL.createObjectURL(await response.blob()) }
