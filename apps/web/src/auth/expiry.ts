import { MessagePlugin } from 'tdesign-vue-next'
import type { Router } from 'vue-router'
import { subscribeAuthSessionExpired } from './events.ts'
import { clearLocalSession } from './session.ts'

let installed = false

export function installAuthExpiryHandler(router: Router) {
  if (installed) return
  installed = true
  subscribeAuthSessionExpired(() => {
    clearLocalSession()
    const current = router.currentRoute.value
    if (current.name === 'login' || current.name === 'register') return
    const redirect = current.fullPath.startsWith('/login') ? '/chat' : current.fullPath
    void router.replace({ name: 'login', query: { redirect, reason: 'expired' } })
    MessagePlugin.warning('登录已过期，请重新登录')
  })
}
