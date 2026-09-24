import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { searchGlobal, searchKnowledge } from '../src/api/rag.ts'

const originalFetch = globalThis.fetch
const originalSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function storage(token = ''): Storage {
  return { getItem: (key) => key === 'access_token' ? token : null, setItem: () => {}, removeItem: () => {} } as Storage
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  globalThis.fetch = originalFetch
  if (originalSessionStorage) Object.defineProperty(globalThis, 'sessionStorage', originalSessionStorage)
  else delete (globalThis as { sessionStorage?: Storage }).sessionStorage
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else delete (globalThis as { localStorage?: Storage }).localStorage
})

test('search fails closed when the current user cannot be resolved, then retries identity lookup', async () => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage('expired-token') })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage() })
  const calls: Array<{ url: string; headers: Headers }> = []
  let identityAvailable = false
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    calls.push({ url, headers: new Headers(init.headers) })
    if (url.endsWith('/auth/me')) return identityAvailable
      ? json({ id: '7d0779ab-9ea4-409e-a51c-842b5b9fb875', email: 'user@example.com', nickname: 'user', status: 'active' })
      : json({ id: 'dev-user' })
    if (url.endsWith('/auth/refresh')) return json({ message: 'authentication required' }, 401)
    if (url.endsWith('/search/global') || url.endsWith('/search/knowledge')) return json({ items: [{ chunk_id: 'chunk-1', content: 'result' }], diagnostics: { candidate_count: 1, authorized_count: 1 } })
    throw new Error(`unexpected request: ${url}`)
  }

  await assert.rejects(searchGlobal({ query: '2025' }), /authenticated user identity is unavailable/)
  assert.equal(calls.some((call) => call.url.endsWith('/search/global')), false)
  assert.equal(calls.some((call) => call.headers.get('X-User-ID') === '00000000-0000-0000-0000-000000000001'), false)

  identityAvailable = true
  const response = await searchGlobal({ query: '2025' })
  assert.equal(response.items.length, 1)
  const searchCall = calls.find((call) => call.url.endsWith('/search/global'))
  assert.ok(searchCall)
  assert.equal(searchCall.headers.get('X-User-ID'), '7d0779ab-9ea4-409e-a51c-842b5b9fb875')

  await searchKnowledge({ query: '2025', knowledgeBaseId: 'ce8f2bb9-0da6-4eed-8e1c-18c2ff62fc89' })
  const knowledgeCall = calls.find((call) => call.url.endsWith('/search/knowledge'))
  assert.ok(knowledgeCall)
  assert.equal(knowledgeCall.headers.get('X-User-ID'), '7d0779ab-9ea4-409e-a51c-842b5b9fb875')
})
