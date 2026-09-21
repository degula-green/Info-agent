import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { refresh } from '../src/api/core-auth.ts'

const originalFetch = globalThis.fetch
const originalSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')

afterEach(() => {
  globalThis.fetch = originalFetch
  if (originalSessionStorage) Object.defineProperty(globalThis, 'sessionStorage', originalSessionStorage)
  else delete (globalThis as { sessionStorage?: Storage }).sessionStorage
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else delete (globalThis as { localStorage?: Storage }).localStorage
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
  else delete (globalThis as { window?: Window }).window
  if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator)
  else delete (globalThis as { navigator?: Navigator }).navigator
})

test('concurrent refresh calls in one process share one request', async () => {
  let calls = 0
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem: () => null, setItem: () => {}, removeItem: () => {} } })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null, setItem: () => {}, removeItem: () => {} } })
  globalThis.fetch = async (input) => {
    calls += 1
    assert.equal(String(input), '/api/core/auth/refresh')
    await gate
    return new Response(JSON.stringify({ access_token: 'access-1', token_type: 'Bearer', expires_at: '2026-09-21T12:00:00Z' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }

  const pending = [refresh(), refresh(), refresh()]
  await Promise.resolve()
  assert.equal(calls, 1)
  release()
  const results = await Promise.all(pending)
  assert.equal(results.length, 3)
  assert.ok(results.every((result) => result.access_token === 'access-1'))
})

test('shared local access token wins over stale tab session token', async () => {
  let calls = 0
  const session = { getItem: (key: string) => key === 'access_token' ? 'stale-session' : null, setItem: () => {}, removeItem: () => {} }
  const local = { getItem: (key: string) => key === 'access_token' ? 'fresh-shared' : null, setItem: () => {}, removeItem: () => {} }
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: session })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: local })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: undefined })
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: undefined })
  globalThis.fetch = async (_input, init = {}) => {
    calls += 1
    assert.equal(new Headers(init.headers).get('Authorization'), 'Bearer fresh-shared')
    return new Response(JSON.stringify({ access_token: 'access-2', token_type: 'Bearer', expires_at: '' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  const result = await refresh()
  assert.equal(calls, 1)
  assert.equal(result.access_token, 'access-2')
})
