import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { authenticatedFetch } from '../src/auth/request.ts'
import { clearLocalSession, ensureFreshToken, getAuthSession, markSessionExpired } from '../src/auth/session.ts'
import { subscribeAuthSessionExpired } from '../src/auth/events.ts'

const originalFetch = globalThis.fetch
const originalSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function storage(initial: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: (key) => { values.delete(key) },
    clear: () => values.clear(),
    key: () => null,
    get length() { return values.size },
  } as Storage
}

function installStorage(values: Record<string, string>) {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage(values) })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage(values) })
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  clearLocalSession()
  globalThis.fetch = originalFetch
  if (originalSessionStorage) Object.defineProperty(globalThis, 'sessionStorage', originalSessionStorage)
  else delete (globalThis as { sessionStorage?: Storage }).sessionStorage
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else delete (globalThis as { localStorage?: Storage }).localStorage
})

test('refreshes an access token before it expires', async () => {
  const expiresAt = Date.now() + 1_000
  installStorage({ access_token: 'expiring-token', access_token_expires_at: String(expiresAt) })
  let refreshCalls = 0
  globalThis.fetch = async (input) => {
    assert.equal(String(input), '/api/core/auth/refresh')
    refreshCalls += 1
    return json({
      access_token: 'fresh-token',
      token_type: 'Bearer',
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    })
  }

  assert.equal(await ensureFreshToken(), 'fresh-token')
  assert.equal(refreshCalls, 1)
  assert.equal(getAuthSession().accessToken, 'fresh-token')
})

test('retries a protected request once after refreshing on 401', async () => {
  installStorage({
    access_token: 'old-token',
    access_token_expires_at: String(Date.now() + 10 * 60_000),
  })
  const authorizations: string[] = []
  let protectedCalls = 0
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url === '/api/core/auth/refresh') {
      return json({
        access_token: 'new-token',
        token_type: 'Bearer',
        expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      })
    }
    protectedCalls += 1
    authorizations.push(new Headers(init.headers).get('Authorization') || '')
    return protectedCalls === 1 ? json({ code: 'AUTH_UNAUTHENTICATED' }, 401) : json({ ok: true })
  }

  const response = await authenticatedFetch('/api/knowledge/v1/connectors')
  assert.equal(response.status, 200)
  assert.deepEqual(authorizations, ['Bearer old-token', 'Bearer new-token'])
})

test('clears local credentials and emits one session expiry event', () => {
  installStorage({ access_token: 'expired-token', access_token_expires_at: String(Date.now() - 1_000) })
  let events = 0
  const unsubscribe = subscribeAuthSessionExpired(() => { events += 1 })
  markSessionExpired('invalid')
  assert.equal(getAuthSession().accessToken, '')
  assert.equal(events, 1)
  unsubscribe()
})
