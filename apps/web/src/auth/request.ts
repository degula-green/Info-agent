import { CoreAuthError } from '../api/core-auth.ts'
import { ensureFreshToken, markSessionExpired, refreshSession } from './session.ts'

const authenticatedRequestTimeoutMs = 8_000

function boundedSignal(signal: AbortSignal | null | undefined, timeoutMs: number) {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, timeoutMs)
  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
    },
  }
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: { retryUnauthorized?: boolean } = {},
): Promise<Response> {
  const retryUnauthorized = options.retryUnauthorized !== false
  const run = async (token: string) => {
    const headers = new Headers(init.headers)
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const bounded = boundedSignal(init.signal, authenticatedRequestTimeoutMs)
    try {
      return await fetch(input, {
        ...init,
        credentials: init.credentials || 'include',
        headers,
        signal: bounded.signal,
      })
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        throw new CoreAuthError('authenticated request timed out', 'request_timeout', 504, true)
      }
      throw error
    } finally {
      bounded.cleanup()
    }
  }

  const token = await ensureFreshToken()
  if (!token) {
    markSessionExpired('expired')
    throw new CoreAuthError('authentication required', 'AUTH_UNAUTHENTICATED', 401, false)
  }
  let response = await run(token)
  if (response.status !== 401 || !retryUnauthorized) {
    if (response.status === 401) markSessionExpired('invalid')
    return response
  }

  let freshToken = ''
  try {
    freshToken = (await refreshSession()).access_token
  } catch {
    return response
  }
  response = await run(freshToken)
  if (response.status === 401) markSessionExpired('invalid')
  return response
}
