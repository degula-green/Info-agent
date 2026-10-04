export type LocalWechatAccount = {
  wxid: string
  db_dir: string
  root?: string
}

export type LocalWechatPairResult = {
  status: string
  device_id: string
  connector_id: string
  wxid: string
}

const env = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(env.VITE_WECHAT_AGENT_URL || 'http://localhost:8091').replace(/\/$/, '')

async function localAgentRequest<T>(path: string, init: { method?: string; body?: string } = {}): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 20000)
  try {
    const response = await fetch(`${baseURL}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      },
      signal: controller.signal,
    })
    const raw = await response.text()
    let body: any = null
    if (raw) {
      try { body = JSON.parse(raw) } catch { body = raw }
    }
    if (!response.ok) {
      const message = typeof body?.detail === 'string'
        ? body.detail
        : typeof body?.message === 'string'
          ? body.message
          : `本机 Agent 请求失败（${response.status}）`
      throw new Error(message)
    }
    return body as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('本机 Agent 响应超时')
    }
    if (error instanceof TypeError) {
      throw new Error('未检测到本机微信 Agent，请先启动 Agent')
    }
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export async function listLocalWechatAccounts(pairingID: string, pairingCode: string) {
  return localAgentRequest<{ items: LocalWechatAccount[]; total: number }>('/local/browser/accounts', {
    method: 'POST',
    body: JSON.stringify({ pairing_id: pairingID, pairing_code: pairingCode }),
  })
}

export async function pairLocalWechatAccount(pairingID: string, pairingCode: string, wxid: string) {
  return localAgentRequest<LocalWechatPairResult>('/local/browser/pair', {
    method: 'POST',
    body: JSON.stringify({
      pairing_id: pairingID,
      pairing_code: pairingCode,
      wxid,
      agent_version: 'desktop-wechat-agent',
    }),
  })
}
