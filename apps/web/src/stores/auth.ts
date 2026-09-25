import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as core from '@/api/core-auth'
const KEY = 'access_token'

// Decode the JWT payload without verifying it. The server remains the source of
// truth for authorization; this only lets the client stop treating an expired
// token as a valid session.
function tokenExpiresAt(token: string): number | null {
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    const normalized = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
    const payload = JSON.parse(atob(padded)) as { exp?: number }
    return typeof payload.exp === 'number' ? payload.exp : null
  } catch {
    return null
  }
}

function isUsable(token: string) {
  if (!token) return false
  const expiresAt = tokenExpiresAt(token)
  // A token without an exp claim cannot be judged locally; treat it as usable
  // and let the API surface a 401 if the server disagrees.
  if (expiresAt == null) return true
  return expiresAt * 1000 > Date.now()
}

function storedToken() { return sessionStorage.getItem(KEY) || localStorage.getItem(KEY) || '' }

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(storedToken())
  const isAuthenticated = computed(() => isUsable(accessToken.value))
  function save(token: string) { accessToken.value = token; sessionStorage.setItem(KEY, token); localStorage.setItem(KEY, token) }
  function clear() { accessToken.value = ''; sessionStorage.removeItem(KEY); localStorage.removeItem(KEY) }
  // Re-read storage so cross-tab logins/logouts and the shared 401 handler are
  // reflected without a full page reload.
  function sync() { const token = storedToken(); if (token !== accessToken.value) accessToken.value = token }
  async function login(email: string, password: string) { const result = await core.login(email, password); save(result.access_token); return result }
  async function register(email: string, username: string, password: string, confirm: string) { return core.register(email, username, password, confirm) }
  async function refresh() { const result = await core.refresh(); save(result.access_token); return result }
  async function logout() { try { await core.logout() } finally { clear() } }
  return { accessToken, isAuthenticated, login, register, refresh, logout, clear, sync }
})
