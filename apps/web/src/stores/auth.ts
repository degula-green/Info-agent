import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as core from '@/api/core-auth'
import {
  clearLocalSession,
  getAuthSession,
  refreshSession,
  saveSessionResult,
  subscribeAuthSession,
  type AuthSessionSnapshot,
} from '@/auth/session'

export const useAuthStore = defineStore('auth', () => {
  const session = ref<AuthSessionSnapshot>(getAuthSession())
  const accessToken = computed(() => session.value.accessToken)
  const isAuthenticated = computed(() => Boolean(accessToken.value))
  subscribeAuthSession((value) => { session.value = value })
  async function login(email: string, password: string) { const result = await core.login(email, password); saveSessionResult(result); return result }
  async function register(email: string, username: string, password: string, confirm: string) { return core.register(email, username, password, confirm) }
  async function refresh() { return refreshSession() }
  async function logout() { try { await core.logout() } finally { clearLocalSession() } }
  return { session, accessToken, isAuthenticated, login, register, refresh, logout }
})
