import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import {
  AgentApiError,
  approveAgentApproval,
  buildScheduleDraft,
  loadScheduleDrafts,
  type AgentApproval,
  type AgentObservation,
  type AgentTask,
} from '../src/api/info-agent.ts'

const originalFetch = globalThis.fetch
const originalSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

const USER_ID = '7d0779ab-9ea4-409e-a51c-842b5b9fb875'

function storage(token = ''): Storage {
  return { getItem: (key) => (key === 'access_token' ? token : null), setItem: () => {}, removeItem: () => {} } as Storage
}

function installStorage(token = 'access-token') {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage(token) })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage() })
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

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return {
    task_id: 'task-1',
    source_type: 'knowledge_event',
    owner_user_id: USER_ID,
    status: 'waiting_approval',
    input: { text: '明天晚上八点开个评审会，会议室 A', source_message_id: 'message-1' },
    source_ref: {
      platform: 'feishu',
      conversation_type: 'group',
      knowledge_item_id: 'item-1',
      conversation_ingestion_id: 'conversation-1',
    },
    created_at: '2026-09-25T10:58:18Z',
    ...overrides,
  }
}

function approval(overrides: Partial<AgentApproval> = {}): AgentApproval {
  return {
    approval_id: 'approval-1',
    task_id: 'task-1',
    plan_id: 'plan-1',
    step_id: 'step-1',
    capability: 'calendar.create',
    arguments: {
      title: '开个评审会，会议室 A',
      time_expression: '明天晚上八点',
      timezone: 'Asia/Shanghai',
      location: '会议室 A',
      owner_user_id: USER_ID,
      idempotency_key: `knowledge_event:${USER_ID}:item-1:1`,
    },
    version: 3,
    status: 'waiting_approval',
    ...overrides,
  }
}

function observation(overrides: Partial<AgentObservation> = {}): AgentObservation {
  return {
    observation_id: 'observation-1',
    task_id: 'task-1',
    plan_id: 'plan-1',
    step_id: 'step-1',
    capability: 'calendar.create',
    status: 'succeeded',
    output: {},
    created_at: '2026-09-25T10:59:00Z',
    ...overrides,
  }
}

test('a waiting approval becomes a confirmable draft with the message and time', () => {
  const draft = buildScheduleDraft({ task: task(), approval: approval() })

  assert.equal(draft.state, 'confirmable')
  assert.equal(draft.title, '开个评审会，会议室 A')
  assert.equal(draft.timeLabel, '明天晚上八点')
  assert.equal(draft.timezone, 'Asia/Shanghai')
  assert.equal(draft.location, '会议室 A')
  assert.equal(draft.sourceLabel, '飞书 · 群聊')
  assert.equal(draft.sourceText, '明天晚上八点开个评审会，会议室 A')
  assert.equal(draft.approvalId, 'approval-1')
  assert.equal(draft.approvalVersion, 3)
})

test('an explicit start time is rendered in the draft timezone', () => {
  const draft = buildScheduleDraft({
    task: task(),
    approval: approval({
      arguments: {
        title: '评审会',
        start_time: '2026-09-26T12:00:00Z',
        end_time: '2026-09-26T13:00:00Z',
        timezone: 'Asia/Shanghai',
      },
    }),
  })

  assert.match(draft.timeLabel, /20:00/)
  assert.match(draft.timeLabel, /→/)
  assert.equal(draft.timeExpression, undefined)
})

test('a waiting_input draft without a calendar authorization asks to bind one', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'waiting_input' }),
    approval: approval({ status: 'approved' }),
    observations: [
      observation({
        output: { requires_user_input: true, missing_information: ['calendar_authorization'], reason: 'calendar_not_bound' },
      }),
    ],
  })

  assert.equal(draft.state, 'needs_calendar')
  assert.deepEqual(draft.missingInformation, ['calendar_authorization'])
})

test('a finished draft carries the created event id and link', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'succeeded' }),
    approval: approval({ status: 'approved' }),
    observations: [
      observation({ output: { event_id: 'evt-9', event_url: 'https://calendar.example/evt-9' } }),
    ],
  })

  assert.equal(draft.state, 'created')
  assert.equal(draft.eventId, 'evt-9')
  assert.equal(draft.eventUrl, 'https://calendar.example/evt-9')
})

test('a draft being confirmed shows the in-flight state first', () => {
  const draft = buildScheduleDraft({ task: task(), approval: approval(), submitting: true })

  assert.equal(draft.state, 'creating')
  // The title and time are still shown while the write is in flight.
  assert.equal(draft.title, '开个评审会，会议室 A')
  assert.equal(draft.timeLabel, '明天晚上八点')
})

test('the in-flight state never overrides a terminal task status', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'succeeded' }),
    approval: approval({ status: 'approved' }),
    observations: [observation({ output: { event_id: 'evt-9' } })],
    submitting: true,
  })

  assert.equal(draft.state, 'created')
})

test('a failed draft surfaces the failure reason', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'failed', last_error: { message: 'calendar provider rejected the request' } }),
    approval: approval({ status: 'approved' }),
    observations: [observation({ status: 'failed', output: {}, error: { message: 'calendar provider rejected the request' } })],
  })

  assert.equal(draft.state, 'failed')
  assert.equal(draft.errorMessage, 'calendar provider rejected the request')
})

test('loading drafts joins tasks, approvals and observations with the agent user header', async () => {
  installStorage()
  const calls: Array<{ url: string; headers: Headers }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    calls.push({ url, headers: new Headers(init.headers) })
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    if (url.includes('/tasks?status=')) {
      return json({
        items: [
          task(),
          task({
            task_id: 'task-2',
            status: 'waiting_input',
            input: { text: '明天下午三点开个会' },
            // No conversation id: the card must fall back to platform · type.
            source_ref: { platform: 'feishu', conversation_type: 'group' },
          }),
        ],
      })
    }
    if (url.endsWith('/approvals')) {
      return json({ items: [approval(), approval({ approval_id: 'approval-2', task_id: 'task-2', status: 'approved', version: 1 })] })
    }
    if (url.endsWith('/connectors/feishu/conversations')) {
      return json({ items: [{ id: 'conversation-1', platform: 'feishu', name: '研发组', conversation_type: 'group' }] })
    }
    if (url.endsWith('/task-2/observations')) {
      return json({ items: [observation({ output: { missing_information: ['calendar_authorization'] } })] })
    }
    throw new Error(`unexpected request: ${url}`)
  }

  const drafts = await loadScheduleDrafts()

  assert.equal(drafts.length, 2)
  assert.deepEqual(drafts.map((draft) => draft.state).sort(), ['confirmable', 'needs_calendar'])
  // The conversation name comes from Knowledge; the type stays as the fallback.
  assert.equal(drafts[0].sourceLabel, '飞书 · 研发组')
  assert.equal(drafts[1].sourceLabel, '飞书 · 群聊')
  const agentCalls = calls.filter((call) => call.url.includes('/api/agent/v1'))
  assert.ok(agentCalls.length >= 3)
  assert.ok(agentCalls.every((call) => call.headers.get('X-Agent-User-Id') === USER_ID))
  assert.ok(agentCalls.some((call) => call.url.includes('status=waiting_approval,waiting_input')))
  // The approved task keeps its approval arguments, so no plan lookup is needed.
  assert.ok(!agentCalls.some((call) => call.url.endsWith('/plan')))
})

test('confirming sends the approval version', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json(approval({ status: 'approved' }))
  }

  await approveAgentApproval('approval-1', 3)

  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'POST')
  assert.ok(calls[0].url.endsWith('/approvals/approval-1/approve'))
  assert.deepEqual(calls[0].body, { version: 3 })
})

test('an unusable identity fails closed instead of listing someone else\'s drafts', async () => {
  installStorage('another-token')
  let agentCalls = 0
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: 'dev-user', email: 'dev@example.com', nickname: 'dev', status: 'active' })
    agentCalls += 1
    return json({ items: [] })
  }

  await assert.rejects(() => loadScheduleDrafts(), (error: unknown) => error instanceof AgentApiError)
  assert.equal(agentCalls, 0)
})
