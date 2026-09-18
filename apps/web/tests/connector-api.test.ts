import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import {
  addConversationCollector,
  attachConversation,
  createLocalUploadTask,
  discoverConversations,
  getConnectors,
  getConversationDetail,
  getFeishuAuthorizeURL,
  getKnowledgeAttachmentContent,
  removeConversationCollector,
  setConversationStatus,
  unbindConnector,
} from '../src/api/info-knowledge.ts'

const originalFetch = globalThis.fetch
const originalSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function storage(token = ''): Storage {
  return { getItem: (key) => key === 'access_token' ? token : null } as Storage
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

test('connector, conversation, and attachment calls use the Knowledge contract', async () => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage('jwt-token') })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage() })
  const calls: Array<{ url: string; method: string; headers: Headers; body: string }> = []
  const conversation = { id: 'c1', platform: 'feishu', platform_workspace_key: 'tenant', external_conversation_id: 'chat', conversation_type: 'group', name: 'Team', ingestion_scope: 'organization', organization_id: 'org', created_by_user_id: 'u1', status: 'active', created_at: '2026-09-05T00:00:00Z', updated_at: '2026-09-05T00:00:00Z' }
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    const method = init.method || 'GET'
    calls.push({ url, method, headers: new Headers(init.headers), body: String(init.body || '') })
    if (url.endsWith('/connectors')) return json({ items: [] })
    if (url.endsWith('/connectors/feishu/authorize')) return json({ authorize_url: 'https://feishu.example/authorize' })
    if (url.endsWith('/conversations/discover')) return json({ discovery_id: 'd1', connector_id: 'a1', platform: 'feishu', expires_at: '2026-09-05T00:10:00Z', conversations: [] })
    if (url.endsWith('/conversations/attach')) return json(conversation, 201)
    if (url.endsWith('/conversations/c1')) return json(conversation)
    if (url.includes('/conversations/c1/messages')) return json({ items: [] })
    if (url.endsWith('/conversations/c1/attachments')) return json({ items: [] })
    if (url.endsWith('/attachments/a1/content')) return new Response('attachment', { status: 200 })
    return json({ status: 'ok', id: 'collector' })
  }

  await getConnectors()
  assert.equal(await getFeishuAuthorizeURL('rebind'), 'https://feishu.example/authorize')
  await unbindConnector('wechat')
  await discoverConversations('feishu')
  await attachConversation({ platform: 'feishu', externalConversationID: 'chat', conversationType: 'group', discoveryID: 'd1', organizationID: 'org', requestedStartAt: '2026-09-01T00:00:00Z' })
  await addConversationCollector('c1')
  await removeConversationCollector('c1', 'collector id')
  await setConversationStatus('c1', 'pause')
  await setConversationStatus('c1', 'resume')
  await getConversationDetail('c1')
  assert.equal(await (await getKnowledgeAttachmentContent('a1', true)).text(), 'attachment')

  assert.ok(calls.every((call) => call.url.startsWith('/api/knowledge/v1/')))
  assert.ok(calls.every((call) => call.headers.get('Authorization') === 'Bearer jwt-token'))
  assert.ok(calls.every((call) => call.headers.get('X-Request-ID')))
  assert.ok(calls.every((call) => call.headers.get('X-Trace-ID')))
  assert.equal(calls.find((call) => call.url.endsWith('/connectors/feishu/authorize'))?.body, '{"intent":"rebind"}')
  assert.ok(calls.some((call) => call.url.endsWith('/conversations/c1/collectors/collector%20id') && call.method === 'DELETE'))
  assert.equal(calls.find((call) => call.url.endsWith('/attachments/a1/content'))?.headers.get('Accept'), 'application/octet-stream')
})

test('attachment content errors preserve the backend permission code', async () => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage('jwt-token') })
  globalThis.fetch = async () => json({ code: 'attachment_content_restricted', message: 'attachment content requires approval', retryable: false }, 403)
  await assert.rejects(getKnowledgeAttachmentContent('protected'), (error: any) => {
    assert.equal(error.code, 'attachment_content_restricted')
    assert.equal(error.status, 403)
    return true
  })
})

test('local upload task includes the required business trace id', async () => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage('jwt-token') })
  let payload: Record<string, unknown> | undefined
  globalThis.fetch = async (_input, init = {}) => {
    payload = JSON.parse(String(init.body || '{}'))
    return json({ request_id: 'request-1' }, 201)
  }

  await createLocalUploadTask({
    requestID: 'request-1',
    traceID: 'trace-1',
    uploadDestination: 'private_local_library',
    fileName: 'notes.txt',
    mimeType: 'text/plain',
    sizeBytes: 5,
    contentHash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  })

  assert.equal(payload?.request_id, 'request-1')
  assert.equal(payload?.trace_id, 'trace-1')
  assert.equal(payload?.content_hash, 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
})

test('bounds concurrent detail loads to avoid a pending-request burst', async () => {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage('jwt-token') })
  let active = 0
  let peak = 0
  let calls = 0
  const conversation = (id: string) => ({ id, platform: 'feishu', external_conversation_id: id, conversation_type: 'group', name: id, ingestion_scope: 'organization', organization_id: 'org', created_at: '2026-09-05T00:00:00Z', updated_at: '2026-09-05T00:00:00Z', message_count: 0, attachment_count: 0 })
  globalThis.fetch = async (input) => {
    const url = String(input)
    calls += 1
    active += 1
    peak = Math.max(peak, active)
    await new Promise((resolve) => setTimeout(resolve, 15))
    active -= 1
    if (/\/conversations\/c[12]$/.test(url)) return json(conversation(url.endsWith('/c1') ? 'c1' : 'c2'))
    if (url.includes('/messages')) return json({ items: [] })
    if (url.includes('/attachments')) return json({ items: [] })
    return json({ items: [] })
  }
  await Promise.all([getConversationDetail('c1'), getConversationDetail('c2')])
  assert.equal(peak <= 2, true, `request peak was ${peak}`)
  assert.equal(calls, 6)
})
