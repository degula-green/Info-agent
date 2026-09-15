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
function requestID() { return globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}` }
export function getAccessToken() { try { return sessionStorage.getItem('access_token') || localStorage.getItem('access_token') || '' } catch { return '' } }
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
export async function refresh() {
  if (refreshPromise) return refreshPromise
  refreshPromise = request<CoreTokenResponse>('/auth/refresh', { method: 'POST' })
    .then((result) => { saveAccessToken(result.access_token); return result })
    .finally(() => { refreshPromise = null })
  return refreshPromise
}
export const logout = () => request<void>('/auth/logout', { method: 'POST' })
export const getCurrentUser = () => request<CoreUser>('/auth/me', { method: 'GET' })
export const updateCurrentUser = (nickname: string) => request<CoreUser>('/auth/me', { method: 'PATCH', body: JSON.stringify({ nickname }) })
export async function uploadAvatar(file: File, retried = false, accessToken?: string): Promise<CoreUser> { const headers = new Headers({ Accept: 'application/json', 'X-Request-ID': requestID(), 'X-Trace-ID': requestID() }); const token = accessToken || getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`); const data = new FormData(); data.append('file', file); const response = await fetch(`${baseURL}/auth/me/avatar`, { method: 'POST', body: data, credentials: 'include', headers }); const raw = await response.text(); let body: any = null; try { body = raw ? JSON.parse(raw) : null } catch { throw new CoreAuthError('头像服务返回了无效响应', 'AUTH_INVALID_RESPONSE', response.status || 502) } if (response.status === 401 && !retried) { try { const refreshed = await refresh(); return uploadAvatar(file, true, refreshed.access_token) } catch { /* fall through */ } } if (!response.ok) throw new CoreAuthError(body?.message || 'avatar upload failed', body?.code || 'request_failed', response.status); return body as CoreUser }
export async function downloadAvatar(retried = false): Promise<string> { const headers = new Headers({ Accept: 'image/*', 'X-Request-ID': requestID(), 'X-Trace-ID': requestID() }); const token = getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`); const response = await fetch(`${baseURL}/auth/me/avatar`, { credentials: 'include', headers }); if (response.status === 401 && !retried) { try { await refresh(); return downloadAvatar(true) } catch { /* fall through */ } } if (!response.ok) throw new CoreAuthError('avatar download failed', 'AVATAR_NOT_FOUND', response.status); return URL.createObjectURL(await response.blob()) }
