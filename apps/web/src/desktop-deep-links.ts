import type { Router } from 'vue-router'

export function installDesktopDeepLinkHandler(router: Router): (() => void) | undefined {
  if (typeof window === 'undefined' || !window.infoAgentDesktop?.onDeepLink) return undefined
  return window.infoAgentDesktop.onDeepLink((link) => {
    if (link.type === 'oauth' && link.provider === 'feishu') {
      router.push({
        name: 'profile',
        query: {
          connector: 'feishu',
          status: link.status || '',
          error: link.errorCode || '',
          state: link.state || '',
        },
      })
      return
    }
    if (link.type === 'organization-invitation' && link.token) {
      router.push({
        name: 'organizationInvitation',
        params: { token: link.token },
      })
    }
  })
}
