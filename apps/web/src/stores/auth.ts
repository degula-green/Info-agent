import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as core from '@/api/core-auth'
const KEY = 'access_token'
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(sessionStorage.getItem(KEY) || localStorage.getItem(KEY) || '')
  const isAuthenticated = computed(() => Boolean(accessToken.value))
  function save(token: string) { accessToken.value = token; sessionStorage.setItem(KEY, token); localStorage.setItem(KEY, token) }
  async function login(email: string, password: string) { const result = await core.login(email, password); save(result.access_token); return result }
  async function register(email: string, username: string, password: string, confirm: string) { return core.register(email, username, password, confirm) }
  async function refresh() { const result = await core.refresh(); save(result.access_token); return result }
  async function logout() { try { await core.logout() } finally { accessToken.value = ''; sessionStorage.removeItem(KEY); localStorage.removeItem(KEY) } }
  return { accessToken, isAuthenticated, login, register, refresh, logout }
})
