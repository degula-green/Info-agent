export interface DesktopRuntimeBridge {
  apiBaseUrl?: string
  webBaseUrl?: string
  invitationBaseUrl?: string
  getStatus?: () => Promise<unknown>
  openExternal?: (url: string) => Promise<void>
  onDeepLink?: (callback: (link: {
    type?: string
    provider?: string
    status?: string
    errorCode?: string
    state?: string
    token?: string
  }) => void) => () => void
  localAgentRequest?: <T>(path: string, init?: { method?: string; body?: string }) => Promise<T>
}

declare global {
  interface Window {
    infoAgentDesktop?: DesktopRuntimeBridge
  }
}

export function desktopApiBase(path: string): string {
  if (typeof window === 'undefined') return ''
  const base = String(window.infoAgentDesktop?.apiBaseUrl || '').replace(/\/$/, '')
  if (!base) return ''
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

export function desktopLocalAgentAvailable(): boolean {
  return typeof window !== 'undefined' && typeof window.infoAgentDesktop?.localAgentRequest === 'function'
}

export function desktopRuntimeActive(): boolean {
  return typeof window !== 'undefined' && Boolean(window.infoAgentDesktop?.apiBaseUrl)
}

export function desktopWebBaseUrl(): string {
  if (typeof window === 'undefined') return ''
  return String(window.infoAgentDesktop?.webBaseUrl || '').replace(/\/$/, '')
}

export function desktopInvitationBaseUrl(): string {
  if (typeof window === 'undefined') return ''
  const bridge = window.infoAgentDesktop
  const configured = String(bridge?.invitationBaseUrl || '').replace(/\/$/, '')
  if (configured) return configured
  const webBaseUrl = desktopWebBaseUrl()
  return webBaseUrl ? `${webBaseUrl}/invite` : ''
}

export async function openExternalUrl(url: string): Promise<boolean> {
  const bridge = typeof window === 'undefined' ? undefined : window.infoAgentDesktop
  if (!bridge?.openExternal) return false
  await bridge.openExternal(url)
  return true
}

export function desktopLocalAgentRequest<T>(
  path: string,
  init: { method?: string; body?: string } = {},
): Promise<T> {
  const bridge = typeof window === 'undefined' ? undefined : window.infoAgentDesktop
  if (!bridge?.localAgentRequest) {
    return Promise.reject(new Error('desktop local agent bridge is unavailable'))
  }
  return bridge.localAgentRequest<T>(path, init)
}
