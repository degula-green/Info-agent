import {
  CoreAuthError,
  clearAccessToken,
  getAccessToken,
  refresh as refreshCoreToken,
  saveAccessToken,
  type CoreTokenResponse,
} from '../api/core-auth.ts'
import { emitAuthSessionExpired } from './events.ts'

const EXPIRES_AT_KEY = 'access_token_expires_at'
const REFRESH_SKEW_MS = 60_000

export type AuthSessionSnapshot = {
  accessToken: string
  expiresAt: number
  refreshing: boolean
}

type Listener = (snapshot: AuthSessionSnapshot) => void

let snapshot: AuthSessionSnapshot = {
  accessToken: '',
  expiresAt: 0,
  refreshing: false,
}
let refreshPromise: Promise<CoreTokenResponse> | null = null
let storageListenerInstalled = false
const listeners = new Set<Listener>()

function readStorageValue(key: string): string {
  let local = ''
  let session = ''
  try { local = typeof localStorage === 'undefined' ? '' : localStorage.getItem(key) || '' } catch { /* unavailable */ }
  try { session = typeof sessionStorage === 'undefined' ? '' : sessionStorage.getItem(key) || '' } catch { /* unavailable */ }
  return local || session
}

function writeStorageValue(key: string, value: string) {
  try { sessionStorage.setItem(key, value) } catch { /* unavailable */ }
  try { localStorage.setItem(key, value) } catch { /* unavailable */ }
}

function removeStorageValue(key: string) {
  try { sessionStorage.removeItem(key) } catch { /* unavailable */ }
  try { localStorage.removeItem(key) } catch { /* unavailable */ }
}

function decodeJWTExpiry(token: string): number {
  try {
    const payload = token.split('.')[1]
    if (!payload) return 0
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const decoded = globalThis.atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '='))
    const value = JSON.parse(decoded) as { exp?: number }
    return Number(value.exp || 0) * 1000
  } catch {
    return 0
  }
}

function readExpiresAt(token: string): number {
  try {
    const raw = readStorageValue(EXPIRES_AT_KEY)
    const numeric = Number(raw || 0)
    if (Number.isFinite(numeric) && numeric > 0) return numeric
    const parsed = Date.parse(raw)
    if (Number.isFinite(parsed) && parsed > 0) return parsed
  } catch {
    // Fall through to JWT expiry.
  }
  return decodeJWTExpiry(token)
}

function readSnapshot(): AuthSessionSnapshot {
  const accessToken = getAccessToken()
  return {
    accessToken,
    expiresAt: accessToken ? readExpiresAt(accessToken) : 0,
    refreshing: snapshot.refreshing,
  }
}

function publish(next: AuthSessionSnapshot) {
  snapshot = next
  for (const listener of listeners) listener(next)
}

function syncFromStorage() {
  const next = readSnapshot()
  if (
    next.accessToken !== snapshot.accessToken
    || next.expiresAt !== snapshot.expiresAt
  ) {
    publish(next)
  }
}

function installStorageListener() {
  if (storageListenerInstalled || typeof window === 'undefined') return
  storageListenerInstalled = true
  window.addEventListener('storage', (event) => {
    if (event.key === 'access_token' || event.key === EXPIRES_AT_KEY) syncFromStorage()
  })
}

export function initializeAuthSession() {
  installStorageListener()
  publish(readSnapshot())
}

export function getAuthSession(): AuthSessionSnapshot {
  syncFromStorage()
  return { ...snapshot }
}

export function subscribeAuthSession(listener: Listener) {
  listeners.add(listener)
  listener(getAuthSession())
  return () => listeners.delete(listener)
}

export function saveSessionResult(result: CoreTokenResponse) {
  saveAccessToken(result.access_token, result.expires_at)
  const expiresAt = Date.parse(result.expires_at || '')
  const resolvedExpiry = Number.isFinite(expiresAt) && expiresAt > 0
    ? expiresAt
    : decodeJWTExpiry(result.access_token)
  if (resolvedExpiry > 0) writeStorageValue(EXPIRES_AT_KEY, String(resolvedExpiry))
  else removeStorageValue(EXPIRES_AT_KEY)
  publish({ accessToken: result.access_token, expiresAt: resolvedExpiry, refreshing: false })
}

export function clearLocalSession() {
  clearAccessToken()
  removeStorageValue(EXPIRES_AT_KEY)
  publish({ accessToken: '', expiresAt: 0, refreshing: false })
}

export function markSessionExpired(reason: 'expired' | 'invalid' | 'refresh_failed') {
  clearLocalSession()
  emitAuthSessionExpired(reason)
}

export async function refreshSession(): Promise<CoreTokenResponse> {
  if (refreshPromise) return refreshPromise
  publish({ ...snapshot, refreshing: true })
  refreshPromise = refreshCoreToken()
    .then((result) => {
      saveSessionResult(result)
      return result
    })
    .catch((error) => {
      publish({ ...readSnapshot(), refreshing: false })
      if (error instanceof CoreAuthError && error.status === 401) {
        markSessionExpired('refresh_failed')
      }
      throw error
    })
    .finally(() => {
      refreshPromise = null
    })
  return refreshPromise
}

export async function ensureFreshToken(skewMs = REFRESH_SKEW_MS): Promise<string> {
  const current = getAuthSession()
  if (!current.accessToken) return ''
  if (!current.expiresAt || current.expiresAt - Date.now() > Math.max(0, skewMs)) {
    return current.accessToken
  }
  try {
    return (await refreshSession()).access_token
  } catch (error) {
    if (error instanceof CoreAuthError && error.status === 401) return ''
    throw error
  }
}
