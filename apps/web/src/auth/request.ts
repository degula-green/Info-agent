import { CoreAuthError } from '../api/core-auth.ts'
import { ensureFreshToken, markSessionExpired, refreshSession } from './session.ts'

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: { retryUnauthorized?: boolean } = {},
): Promise<Response> {
  const retryUnauthorized = options.retryUnauthorized !== false
  const run = async (token: string) => {
    const headers = new Headers(init.headers)
    if (token) headers.set('Authorization', `Bearer ${token}`)
    return fetch(input, {
      ...init,
      credentials: init.credentials || 'include',
      headers,
    })
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
