import assert from 'node:assert/strict'
import { afterEach, mock, test } from 'node:test'
import {
  agentTodoSourceInitial,
  agentTodoSourceLabel,
  agentTodoTimeLabel,
  approveAgentApproval,
  buildScheduleDraft,
  cancelAgentTask,
  completeAgentTodo,
  composeEditedArguments,
  composeTimeFixText,
  createAgentConversation,
  createAgentTask,
  dayOfISO,
  deleteAgentConversation,
  deleteAgentTodo,
  draftSortKey,
  endOfDayISO,
  getAgentAnswerSnapshot,
  getAgentConversation,
  listAgentConversations,
  listAgentTodos,
  loadScheduleDrafts,
  rejectAgentApproval,
  renameAgentConversation,
  sortAgentTodos,
  streamAgentTaskEvents,
  submitAgentTaskInput,
  updateAgentTodo,
  uploadAgentAttachment,
  type AgentApproval,
  type AgentObservation,
  type AgentTask,
  type AgentTaskEvent,
  type AgentTodo,
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

function sse(...blocks: string[]) {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const block of blocks) controller.enqueue(encoder.encode(block))
        controller.close()
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
}

function taskEvent(sequence: number, eventType: string, payload: Record<string, unknown> = {}) {
  return `id: ${sequence}\nevent: ${eventType}\ndata: ${JSON.stringify({
    event_id: `event-${sequence}`,
    task_id: 'task-1',
    sequence,
    event_type: eventType,
    payload,
    occurred_at: '2026-09-29T10:00:00Z',
  })}\n\n`
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
      sender_display_name: '张三',
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
    capability: 'todo.create',
    arguments: {
      title: '开个评审会，会议室 A',
      due_expression: '明天晚上八点',
      timezone: 'Asia/Shanghai',
      notes: '会议室 A',
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
    capability: 'todo.create',
    status: 'succeeded',
    output: {},
    created_at: '2026-09-25T10:59:00Z',
    ...overrides,
  }
}

function todo(overrides: Partial<AgentTodo> = {}): AgentTodo {
  return {
    todo_id: 'todo-1',
    owner_user_id: USER_ID,
    title: '提交周报',
    due_at: '2026-10-05T07:59:00Z',
    due_expression: '下周一',
    timezone: 'Asia/Shanghai',
    notes: null,
    status: 'open',
    source: { sender_display_name: '张三', conversation_type: 'group' },
    created_at: '2026-10-01T08:00:00Z',
    updated_at: '2026-10-01T08:00:00Z',
    completed_at: null,
    ...overrides,
  }
}

test('a waiting approval becomes a confirmable draft with an unresolved deadline', () => {
  const draft = buildScheduleDraft({ task: task(), approval: approval() })

  assert.equal(draft.state, 'confirmable')
  assert.equal(draft.title, '开个评审会，会议室 A')
  assert.equal(draft.timeLabel, '未设置截止时间')
  assert.equal(draft.timezone, 'Asia/Shanghai')
  assert.equal(draft.notes, '会议室 A')
  assert.equal(draft.senderLabel, '张三')
  assert.equal(draft.sourceLabel, '飞书 · 群聊')
  assert.equal(draft.sourceText, '明天晚上八点开个评审会，会议室 A')
  assert.equal(draft.approvalId, 'approval-1')
  assert.equal(draft.approvalVersion, 3)
})

test('the sender falls back to the approval arguments when the task has none', () => {
  const draft = buildScheduleDraft({
    task: task({ source_ref: { platform: 'feishu', conversation_type: 'group' } }),
    approval: approval({
      arguments: {
        title: '评审会',
        timezone: 'Asia/Shanghai',
        source: { sender_display_name: '李四' },
      },
    }),
  })

  assert.equal(draft.senderLabel, '李四')
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

test('a waiting_input draft asks for the missing time', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'waiting_input' }),
    approval: approval({ status: 'approved' }),
    observations: [
      observation({
        output: { requires_user_input: true, missing_information: ['due_at'], reason: 'no_time' },
      }),
    ],
  })

  assert.equal(draft.state, 'needs_input')
  assert.deepEqual(draft.missingInformation, ['due_at'])
})

test('a finished draft carries the created event id and link', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'succeeded' }),
    approval: approval({ status: 'approved' }),
    observations: [
      observation({ output: { todo_id: 'todo-9' } }),
    ],
  })

  assert.equal(draft.state, 'created')
  assert.equal(draft.eventId, 'todo-9')
})

test('a draft being confirmed shows the in-flight state first', () => {
  const draft = buildScheduleDraft({ task: task(), approval: approval(), submitting: true })

  assert.equal(draft.state, 'creating')
  // The title and time are still shown while the write is in flight.
  assert.equal(draft.title, '开个评审会，会议室 A')
  assert.equal(draft.timeLabel, '未设置截止时间')
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

test('loading drafts joins tasks, approvals and observations with bearer authentication', async () => {
  installStorage()
  const calls: Array<{ url: string; headers: Headers }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    calls.push({ url, headers: new Headers(init.headers) })
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
      return json({ items: [observation({ output: { missing_information: ['due_at'] } })] })
    }
    throw new Error(`unexpected request: ${url}`)
  }

  const drafts = await loadScheduleDrafts()

  assert.equal(drafts.length, 2)
  assert.deepEqual(drafts.map((draft) => draft.state).sort(), ['confirmable', 'needs_input'])
  // The conversation name comes from Knowledge; the type stays as the fallback.
  assert.equal(drafts[0].sourceLabel, '飞书 · 研发组')
  assert.equal(drafts[1].sourceLabel, '飞书 · 群聊')
  const agentCalls = calls.filter((call) => call.url.includes('/api/agent/v1'))
  assert.ok(agentCalls.length >= 3)
  assert.ok(agentCalls.every((call) => call.headers.get('Authorization') === 'Bearer access-token'))
  assert.ok(agentCalls.every((call) => call.headers.get('X-Agent-User-Id') === null))
  assert.ok(agentCalls.some((call) => call.url.includes('status=waiting_approval,waiting_input')))
  // The approved task keeps its approval arguments, so no plan lookup is needed.
  assert.ok(!agentCalls.some((call) => call.url.endsWith('/plan')))
})

test('agent requests refresh once and retry without a client identity header', async () => {
  installStorage('old-token')
  const authorizations: string[] = []
  let taskCalls = 0
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/api/core/auth/refresh')) {
      return json({
        access_token: 'new-token',
        token_type: 'Bearer',
        expires_at: '2026-10-01T12:00:00Z',
      })
    }
    if (url.endsWith('/api/agent/v1/tasks')) {
      taskCalls += 1
      const headers = new Headers(init.headers)
      authorizations.push(headers.get('Authorization') || '')
      assert.equal(headers.get('X-Agent-User-Id'), null)
      return taskCalls === 1
        ? json({ code: 'AUTH_UNAUTHENTICATED' }, 401)
        : json({ task_id: 'task-1', status: 'received', events_url: '/events' }, 202)
    }
    throw new Error(`unexpected request: ${url}`)
  }

  const result = await createAgentTask({ text: '测试认证刷新' })

  assert.equal(result.task_id, 'task-1')
  assert.deepEqual(authorizations, ['Bearer old-token', 'Bearer new-token'])
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

test('a to-do draft renders due_at as the deadline instead of an unset deadline', () => {
  const draft = buildScheduleDraft({
    task: task({ source_ref: {} }),
    approval: approval({
      capability: 'todo.create',
      arguments: {
        title: '我要洗衣服',
        due_expression: '明天',
        due_at: '2026-09-29T15:59:00Z',
        timezone: 'Asia/Shanghai',
        owner_user_id: USER_ID,
        idempotency_key: `knowledge_event:${USER_ID}:item-1:1`,
      },
    }),
  })

  // 2026-09-29T15:59Z is 23:59 in Asia/Shanghai: the end of the day it is due.
  assert.match(draft.timeLabel, /23:59/)
  assert.notEqual(draft.timeLabel, '未设置截止时间')
  assert.equal(draft.dueAt, '2026-09-29T15:59:00Z')
  assert.equal(draft.timeExpression, '明天')
})

test('a to-do without a time says no deadline is set', () => {
  const draft = buildScheduleDraft({
    task: task(),
    approval: approval({
      capability: 'todo.create',
      arguments: { title: '完成登录模块代码', due_expression: '', timezone: 'Asia/Shanghai' },
    }),
  })

  assert.equal(draft.timeLabel, '未设置截止时间')
  assert.equal(draft.dueAt, undefined)
})

test('drafts sort by the to-do deadline', () => {
  const soon = buildScheduleDraft({
    task: task({ task_id: 'a' }),
    approval: approval({ arguments: { title: 'A', due_at: '2026-09-29T15:59:00Z' } }),
  })
  const later = buildScheduleDraft({
    task: task({ task_id: 'b' }),
    approval: approval({ arguments: { title: 'B', due_at: '2026-10-11T15:59:00Z' } }),
  })
  const undated = buildScheduleDraft({
    task: task({ task_id: 'c' }),
    approval: approval({ arguments: { title: 'C' } }),
  })

  assert.ok(draftSortKey(soon) < draftSortKey(later))
  assert.equal(draftSortKey(undated), Number.MAX_SAFE_INTEGER)
})

test('endOfDayISO resolves the last minute of the day in the draft timezone', () => {
  assert.equal(endOfDayISO('2026-09-29', 'Asia/Shanghai'), '2026-09-29T15:59:00.000Z')
  assert.equal(endOfDayISO('2026-01-01', 'Asia/Shanghai'), '2026-01-01T15:59:00.000Z')
  assert.equal(endOfDayISO('not-a-day', 'Asia/Shanghai'), '')
})

test('dayOfISO reads the calendar day back in the draft timezone', () => {
  assert.equal(dayOfISO('2026-09-29T15:59:00Z', 'Asia/Shanghai'), '2026-09-29')
  // 16:00Z is already the next day in Shanghai.
  assert.equal(dayOfISO('2026-09-29T16:00:00Z', 'Asia/Shanghai'), '2026-09-30')
  assert.equal(dayOfISO(undefined, 'Asia/Shanghai'), '')
})

test('composeEditedArguments pins the resolved day and drops the stale phrase', () => {
  const draft = buildScheduleDraft({
    task: task(),
    approval: approval({
      capability: 'todo.create',
      arguments: { title: '下周洗衣服', due_expression: '下周', due_at: '2026-10-11T15:59:00Z', timezone: 'Asia/Shanghai' },
    }),
  })

  const { arguments: edited } = composeEditedArguments(draft, { title: '洗衣服', dueDate: '2026-09-30' })

  assert.equal(edited.due_at, '2026-09-30T15:59:00.000Z')
  // The phrase must not survive: the backend would re-resolve "下周" against the
  // message timestamp and overwrite the day the owner just picked.
  assert.equal(edited.due_expression, null)
  assert.equal(edited.timezone, 'Asia/Shanghai')
})

test('composeEditedArguments clears both fields when the owner unsets the date', () => {
  const draft = buildScheduleDraft({
    task: task(),
    approval: approval({
      capability: 'todo.create',
      arguments: { title: '洗衣服', due_expression: '下周', due_at: '2026-10-11T15:59:00Z', timezone: 'Asia/Shanghai' },
    }),
  })

  const { arguments: edited } = composeEditedArguments(draft, { title: '洗衣服', dueDate: '' })

  assert.equal(edited.due_at, null)
  assert.equal(edited.due_expression, null)
  // Keeping the keys present is what lets the backend null the column.
  assert.ok('due_at' in edited)
  assert.ok('due_expression' in edited)
})

test('confirming with edited arguments posts them with the approval version', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: any }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json(approval({ status: 'approved' }))
  }

  await approveAgentApproval('approval-1', 3, { title: '改过的标题', due_at: '2026-09-30T15:59:00.000Z', due_expression: null })

  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'POST')
  assert.ok(calls[0].url.endsWith('/approvals/approval-1/approve'))
  assert.deepEqual(calls[0].body, {
    version: 3,
    arguments: { title: '改过的标题', due_at: '2026-09-30T15:59:00.000Z', due_expression: null },
  })
})

test('an Agent request without a session fails closed before calling the API', async () => {
  installStorage('')
  let agentCalls = 0
  globalThis.fetch = async () => {
    agentCalls += 1
    return json({ items: [] })
  }

  await assert.rejects(
    () => loadScheduleDrafts(),
    (error: any) => error?.status === 401 && error?.code === 'AUTH_UNAUTHENTICATED',
  )
  assert.equal(agentCalls, 0)
})

test('a waiting_input task keeps asking for the missing time while a fix is in flight', () => {
  const draft = buildScheduleDraft({
    task: task({ status: 'waiting_input' }),
    approval: approval({ status: 'approved' }),
    observations: [
      observation({
        output: { requires_user_input: true, missing_information: ['start_time'], reason: 'start_time_unresolved' },
      }),
    ],
    submitting: true,
  })

  assert.equal(draft.state, 'needs_input')
  assert.deepEqual(draft.missingInformation, ['start_time'])
})

test('an executing task is fetched by id so the card stays visible', async () => {
  installStorage()
  const calls: string[] = []
  globalThis.fetch = async (input) => {
    const url = String(input)
    calls.push(url)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    if (url.includes('/tasks?status=')) return json({ items: [] })
    if (url.endsWith('/approvals')) {
      return json({ items: [approval({ approval_id: 'approval-2', task_id: 'task-2', status: 'approved' })] })
    }
    if (url.endsWith('/tasks/task-2/observations')) return json({ items: [] })
    if (url.endsWith('/tasks/task-2')) return json(task({ task_id: 'task-2', status: 'executing' }))
    throw new Error(`unexpected request: ${url}`)
  }

  const drafts = await loadScheduleDrafts({ trackedTaskIDs: ['task-2'] })

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].taskId, 'task-2')
  assert.equal(drafts[0].state, 'creating')
  // The approved arguments still describe the event while it is being written.
  assert.equal(drafts[0].title, '开个评审会，会议室 A')
  assert.ok(calls.some((url) => url.endsWith('/tasks/task-2')))
})

test('a tracked task that finished comes back with its event id', async () => {
  installStorage()
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    if (url.includes('/tasks?status=')) return json({ items: [] })
    if (url.endsWith('/approvals')) {
      return json({ items: [approval({ approval_id: 'approval-2', task_id: 'task-2', status: 'approved' })] })
    }
    if (url.endsWith('/tasks/task-2/observations')) {
      return json({ items: [observation({ output: { event_id: 'evt-9', event_url: 'https://calendar.example/evt-9' } })] })
    }
    if (url.endsWith('/tasks/task-2')) return json(task({ task_id: 'task-2', status: 'succeeded' }))
    throw new Error(`unexpected request: ${url}`)
  }

  const drafts = await loadScheduleDrafts({ trackedTaskIDs: ['task-2'] })

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].state, 'created')
  assert.equal(drafts[0].eventId, 'evt-9')
})

test('composeTimeFixText only swaps the time phrase', () => {
  assert.equal(composeTimeFixText('明天八点开会', '明天八点', '明天晚上八点'), '明天晚上八点开会')
  assert.equal(composeTimeFixText('开会', '', '明天晚上八点'), '明天晚上八点 开会')
  assert.equal(composeTimeFixText('明天八点开会', '明天八点', '   '), '明天八点开会')
})

test('a re-planned approval wins over the already accepted one', async () => {
  installStorage()
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    if (url.includes('/tasks?status=')) {
      return json({ items: [task({ task_id: 'task-1', status: 'waiting_approval' })] })
    }
    if (url.endsWith('/approvals')) {
      // Versions restart at 1 after a re-plan, so both approvals carry version 1.
      return json({
        items: [
          approval({ approval_id: 'approval-old', status: 'approved', version: 1 }),
          approval({
            approval_id: 'approval-new',
            status: 'waiting_approval',
            version: 1,
            arguments: {
              title: '开会',
              time_expression: '明天上午12点',
              timezone: 'Asia/Shanghai',
              owner_user_id: USER_ID,
              idempotency_key: `knowledge_event:${USER_ID}:item-1:1`,
            },
          }),
        ],
      })
    }
    throw new Error(`unexpected request: ${url}`)
  }

  const drafts = await loadScheduleDrafts()

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].state, 'confirmable')
  assert.equal(drafts[0].approvalId, 'approval-new')
  assert.equal(drafts[0].timeExpression, '明天上午12点')
})

test('submitting a fix posts the repaired sentence as task input', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json(task({ task_id: 'task-2', status: 'planning' }))
  }

  await submitAgentTaskInput('task-2', { text: '明天晚上八点开会' })

  assert.equal(calls.length, 1)
  assert.ok(calls[0].url.endsWith('/tasks/task-2/input'))
  assert.equal(calls[0].method, 'POST')
  assert.deepEqual(calls[0].body, { text: '明天晚上八点开会' })
})

test('deleting a pending schedule cancels its task', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET') })
    return json(task({ task_id: 'task-2', status: 'cancelled' }))
  }

  const cancelled = await cancelAgentTask('task-2')

  assert.equal(cancelled.status, 'cancelled')
  assert.equal(calls.length, 1)
  assert.ok(calls[0].url.endsWith('/tasks/task-2/cancel'))
  assert.equal(calls[0].method, 'POST')
})

test('an expired but unchanged approval keeps its card confirmable', async () => {
  const calls: Array<{ url: string }> = []
  installStorage()
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push({ url })
    installStorage()
    if (url.endsWith('/tasks?status=waiting_approval,waiting_input&limit=20')) {
      return json({ items: [task({ task_id: 'task-1', status: 'waiting_approval' })] })
    }
    if (url.endsWith('/approvals')) {
      return json({ items: [approval({ status: 'expired' })] })
    }
    if (url.includes('/conversations')) return json({ items: [] })
    return json({}, 404)
  }) as typeof fetch

  const drafts = await loadScheduleDrafts()

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].state, 'confirmable')
  assert.equal(drafts[0].approvalId, 'approval-1')
})

test('a write the Agent cannot reconcile warns before the owner confirms', async () => {
  installStorage()
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input)
    installStorage()
    if (url.endsWith('/tasks?status=waiting_approval,waiting_input&limit=20')) {
      return json({ items: [task({ task_id: 'task-1', status: 'waiting_approval' })] })
    }
    if (url.endsWith('/approvals')) {
      return json({ items: [approval({ reconcilable: false })] })
    }
    if (url.includes('/conversations')) return json({ items: [] })
    return json({}, 404)
  }) as typeof fetch

  const drafts = await loadScheduleDrafts()

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].state, 'confirmable')
  assert.match(drafts[0].verificationWarning || '', /无法自动确认结果/)
})

test('a reconcilable write carries no warning', async () => {
  installStorage()
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input)
    installStorage()
    if (url.endsWith('/tasks?status=waiting_approval,waiting_input&limit=20')) {
      return json({ items: [task({ task_id: 'task-1', status: 'waiting_approval' })] })
    }
    if (url.endsWith('/approvals')) {
      return json({ items: [approval({ reconcilable: true })] })
    }
    if (url.includes('/conversations')) return json({ items: [] })
    return json({}, 404)
  }) as typeof fetch

  const drafts = await loadScheduleDrafts()

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].verificationWarning, undefined)
})

test('creating a chat task posts the Agent task contract', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: any }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json({ task_id: 'task-1', status: 'received', events_url: '/api/agent/v1/tasks/task-1/events' }, 202)
  }

  const created = await createAgentTask({ text: '明天晚上八点开评审会', clientMessageId: 'client-1' })

  assert.equal(created.task_id, 'task-1')
  assert.equal(calls.length, 1)
  assert.ok(calls[0].url.endsWith('/api/agent/v1/tasks'))
  assert.equal(calls[0].method, 'POST')
  assert.deepEqual(calls[0].body, {
    text: '明天晚上八点开评审会',
    attachment_ids: [],
    source_type: 'chat',
    client_message_id: 'client-1',
    source_ref: {},
    constraints: {},
  })
})

test('creating a task with an attachment sends attachment_ids', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: any }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json({ task_id: 'task-1', status: 'received', events_url: '/api/agent/v1/tasks/task-1/events' }, 202)
  }

  await createAgentTask({ text: '根据这个附件创建日程', attachmentIds: ['attachment-1'], clientMessageId: 'client-2' })

  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].body, {
    text: '根据这个附件创建日程',
    attachment_ids: ['attachment-1'],
    source_type: 'chat',
    client_message_id: 'client-2',
    source_ref: {},
    constraints: {},
  })
})

test('a follow-up task carries its conversation id', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: any }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json({
      task_id: 'task-2',
      status: 'received',
      events_url: '/api/agent/v1/tasks/task-2/events',
      conversation_id: 'conversation-1',
    }, 202)
  }

  const created = await createAgentTask({
    text: '继续查一下',
    conversationId: 'conversation-1',
    clientMessageId: 'client-2',
  })

  assert.equal(created.conversation_id, 'conversation-1')
  assert.deepEqual(calls[0].body, {
    text: '继续查一下',
    attachment_ids: [],
    source_type: 'chat',
    client_message_id: 'client-2',
    conversation_id: 'conversation-1',
    source_ref: {},
    constraints: {},
  })
})

test('conversation history API uses the Agent conversation contract', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: any }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    if (init.method === 'DELETE') return new Response(null, { status: 204 })
    if (url.includes('/conversations?')) {
      return json({
        items: [{ conversation_id: 'conversation-1', title: '青云官网', status: 'active', last_message_at: '2026-10-02T10:00:00Z', message_count: 2 }],
        page: 1,
        page_size: 20,
        total: 1,
      })
    }
    if (init.method === 'POST') {
      return json({ conversation_id: 'conversation-2', title: '新会话', status: 'active', message_count: 0 })
    }
    if (init.method === 'PATCH') {
      return json({ conversation_id: 'conversation-1', title: '改名后', status: 'active', message_count: 2 })
    }
    return json({
      conversation_id: 'conversation-1',
      owner_user_id: USER_ID,
      title: '青云官网',
      status: 'active',
      source: 'agent',
      summary_cursor: 0,
      message_count: 2,
      messages: [
        { message_id: 'message-1', conversation_id: 'conversation-1', role: 'user', content: '第一问', status: 'completed', task_id: 'task-1', created_at: '2026-10-02T09:59:00Z', updated_at: '2026-10-02T09:59:00Z' },
        { message_id: 'message-2', conversation_id: 'conversation-1', role: 'assistant', content: '回答', status: 'completed', task_id: 'task-1', citations: [], created_at: '2026-10-02T10:00:00Z', updated_at: '2026-10-02T10:00:00Z' },
      ],
      created_at: '2026-10-02T09:59:00Z',
      updated_at: '2026-10-02T10:00:00Z',
    })
  }

  const listed = await listAgentConversations(1, 20)
  const detail = await getAgentConversation('conversation-1')
  const created = await createAgentConversation('新会话')
  const renamed = await renameAgentConversation('conversation-1', '改名后')
  await deleteAgentConversation('conversation-1')

  assert.equal(listed.items[0].conversation_id, 'conversation-1')
  assert.equal(detail.messages[1].content, '回答')
  assert.equal(created.conversation_id, 'conversation-2')
  assert.equal(renamed.title, '改名后')
  assert.ok(calls.some((call) => call.method === 'DELETE' && call.url.endsWith('/conversations/conversation-1')))
})

test('uploading an attachment posts multipart without a JSON content type', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; headers: Headers; body: any }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), headers: new Headers(init.headers), body: init.body })
    return json({
      attachment_id: 'attachment-1',
      file_name: 'meeting.docx',
      mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      size_bytes: 3,
    })
  }

  const file = new File([new Uint8Array([1, 2, 3])], 'meeting.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  })
  const uploaded = await uploadAgentAttachment(file)

  assert.equal(uploaded.attachment_id, 'attachment-1')
  assert.equal(calls.length, 1)
  assert.ok(calls[0].url.endsWith('/api/agent/v1/attachments'))
  assert.equal(calls[0].method, 'POST')
  // The browser must own the multipart boundary: setting application/json here
  // would make the upload unparsable on the server.
  assert.equal(calls[0].headers.get('Content-Type'), null)
  // Uploads go through the shared authenticated fetch: bearer token only.
  assert.equal(calls[0].headers.get('X-Agent-User-Id'), null)
  assert.equal(calls[0].headers.get('Authorization'), 'Bearer access-token')
  assert.ok(calls[0].body instanceof FormData)
  assert.ok((calls[0].body as FormData).get('file') instanceof File)
})

test('Agent SSE parsing resumes after the last sequence', async () => {
  installStorage()
  const urls: string[] = []
  let streamCalls = 0
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    urls.push(url)
    streamCalls += 1
    if (streamCalls === 1) return sse(taskEvent(1, 'task.planning'))
    return sse(taskEvent(2, 'task.completed', { answer: '完成' }))
  }

  const seen: AgentTaskEvent[] = []
  const last = await streamAgentTaskEvents('task-1', {
    onEvent: (event) => {
      seen.push(event)
      return event.event_type !== 'task.completed'
    },
  }, { maxReconnects: 1 })

  assert.equal(last, 2)
  assert.deepEqual(seen.map((event) => event.event_type), ['task.planning', 'task.completed'])
  assert.equal(seen[1].payload.answer, '完成')
  assert.match(urls[1], /after=1/)
})

test('Agent SSE reports a healthy segment end as waiting, not reconnect', async () => {
  installStorage()
  let streamCalls = 0
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    streamCalls += 1
    if (streamCalls === 1) return sse(taskEvent(1, 'task.planning'))
    return sse(taskEvent(2, 'task.completed', { answer: '完成' }))
  }

  const waiting: number[] = []
  const reconnects: number[] = []
  await streamAgentTaskEvents(
    'task-1',
    {
      onEvent: (event) => event.event_type !== 'task.completed',
      onWaiting: (segment) => waiting.push(segment),
      onReconnect: (attempt) => reconnects.push(attempt),
    },
    { maxReconnects: 1 },
  )

  assert.deepEqual(waiting, [1])
  assert.deepEqual(reconnects, [])
})

test('Agent SSE reports a request failure as reconnect, not waiting', async () => {
  installStorage()
  let streamCalls = 0
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    streamCalls += 1
    if (streamCalls === 1) return json({ error: 'temporary' }, 503)
    return sse(taskEvent(2, 'task.completed', { answer: '完成' }))
  }

  const waiting: number[] = []
  const reconnects: number[] = []
  await streamAgentTaskEvents(
    'task-1',
    {
      onEvent: (event) => event.event_type !== 'task.completed',
      onWaiting: (segment) => waiting.push(segment),
      onReconnect: (attempt) => reconnects.push(attempt),
    },
    { maxReconnects: 1 },
  )

  assert.deepEqual(waiting, [])
  assert.deepEqual(reconnects, [1])
})

test('Agent SSE is not cut off by the generic authenticated request timeout', async () => {
  installStorage()
  const controller = new AbortController()
  const reconnects: number[] = []
  let streamStarted = false

  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    streamStarted = true
    return new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => {
        const error = new Error('aborted')
        error.name = 'AbortError'
        reject(error)
      }, { once: true })
    })
  }

  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const pending = streamAgentTaskEvents(
      'task-1',
      {
        onEvent: () => true,
        onReconnect: (attempt) => reconnects.push(attempt),
      },
      { timeoutSeconds: 30, maxReconnects: 1, signal: controller.signal },
    )

    await Promise.resolve()
    await Promise.resolve()
    assert.equal(streamStarted, true)

    mock.timers.tick(8_500)
    await Promise.resolve()
    assert.deepEqual(reconnects, [])

    controller.abort()
    await pending
  } finally {
    mock.timers.reset()
  }
})

test('Agent SSE tracks the answer cursor separately from the task cursor', async () => {
  installStorage()
  const urls: string[] = []
  const cursorStates: Array<{ answerId: string; answerAfter: number }> = []
  let streamCalls = 0
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    urls.push(url)
    streamCalls += 1
    if (streamCalls === 1) {
      const delta = (seq: number, value: string) =>
        `id: 0\nevent: answer.delta\ndata: ${JSON.stringify({
          task_id: 'task-1',
          sequence: 0,
          event_type: 'answer.delta',
          payload: { answer_id: 'a1', step_id: 'step-1', attempt: 1, seq, offset: seq, delta: value },
        })}\n\n`
      return sse(
        taskEvent(2, 'answer.started', {
          answer_id: 'a1',
          step_id: 'step-1',
          attempt: 1,
          next_seq: 1,
        }),
        delta(1, 'he'),
        delta(2, 'llo'),
      )
    }
    return sse(taskEvent(3, 'task.completed', { answer: 'hello' }))
  }

  const seen: string[] = []
  await streamAgentTaskEvents(
    'task-1',
    {
      onEvent: (event) => {
        seen.push(event.event_type)
        return event.event_type !== 'task.completed'
      },
      onAnswerCursor: (state) => cursorStates.push({ ...state }),
    },
    { maxReconnects: 1 },
  )

  assert.deepEqual(seen, ['answer.started', 'answer.delta', 'answer.delta', 'task.completed'])
  // The PG cursor advances from answer.started but never from sequence-0 deltas.
  assert.match(urls[1], /after=2/)
  assert.match(urls[1], /answer_id=a1/)
  assert.match(urls[1], /answer_after=2/)
  assert.equal(cursorStates.at(-1)?.answerAfter, 2)
})

test('Agent answer snapshot uses the recovery endpoint', async () => {
  installStorage()
  const urls: string[] = []
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    urls.push(url)
    return json({ answer_id: 'a1', text: 'hello', next_seq: 2, completed: false, final_seq: 0 })
  }

  const snapshot = await getAgentAnswerSnapshot('task-1', 'a1')

  assert.equal(snapshot.text, 'hello')
  assert.ok(urls[0].endsWith('/tasks/task-1/answers/a1'))
})

test('rejecting an approval posts its optimistic version', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET'), body: init.body ? JSON.parse(String(init.body)) : null })
    return json(approval({ status: 'rejected' }))
  }

  await rejectAgentApproval('approval-1', 3)

  assert.equal(calls.length, 1)
  assert.ok(calls[0].url.endsWith('/approvals/approval-1/reject'))
  assert.equal(calls[0].method, 'POST')
  assert.deepEqual(calls[0].body, { version: 3 })
})

test('reading the to-do ledger asks for open and done rows', async () => {
  installStorage()
  const calls: string[] = []
  globalThis.fetch = async (input) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push(url)
    return json({ items: [todo(), todo({ todo_id: 'todo-2', status: 'done' })] })
  }

  const items = await listAgentTodos(['open', 'done'])

  assert.equal(items.length, 2)
  assert.equal(calls.length, 1)
  const requestURL = new URL(calls[0], 'http://localhost')
  assert.equal(requestURL.pathname, '/api/agent/v1/todos')
  assert.equal(requestURL.searchParams.get('status'), 'open,done')
})

test('completing and restoring a to-do patches only its status', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string; body: unknown }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    const body = init.body ? JSON.parse(String(init.body)) : null
    calls.push({ url, method: String(init.method || 'GET'), body })
    return json(todo({ status: body?.status || 'open', completed_at: body?.status === 'done' ? '2026-10-02T01:00:00Z' : null }))
  }

  await completeAgentTodo('todo-1')
  await updateAgentTodo('todo-1', { status: 'open' })

  assert.equal(calls.length, 2)
  assert.ok(calls[0].url.endsWith('/todos/todo-1'))
  assert.equal(calls[0].method, 'PATCH')
  assert.deepEqual(calls[0].body, { status: 'done' })
  assert.deepEqual(calls[1].body, { status: 'open' })
})

test('deleting a to-do uses the explicit delete endpoint', async () => {
  installStorage()
  const calls: Array<{ url: string; method: string }> = []
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input)
    if (url.endsWith('/auth/me')) return json({ id: USER_ID, email: 'user@example.com', nickname: 'user', status: 'active' })
    calls.push({ url, method: String(init.method || 'GET') })
    return new Response(null, { status: 204 })
  }

  await deleteAgentTodo('todo-1')

  assert.equal(calls.length, 1)
  assert.ok(calls[0].url.endsWith('/todos/todo-1'))
  assert.equal(calls[0].method, 'DELETE')
})

test('real to-do labels preserve the ledger phrase and source', () => {
  const now = new Date('2026-10-02T00:00:00Z')
  assert.equal(agentTodoTimeLabel(todo({ due_at: '2026-10-02T08:00:00Z' }), now), '今天')
  assert.equal(agentTodoTimeLabel(todo({ due_at: '2026-10-03T08:00:00Z' }), now), '明天')
  assert.equal(
    agentTodoTimeLabel(todo({ due_at: null, due_expression: '下周三下午' }), now),
    '下周三下午',
  )
  assert.equal(agentTodoTimeLabel(todo({ due_at: null, due_expression: null }), now), '无时间')
  assert.equal(agentTodoSourceLabel(todo()), '张三')
  assert.equal(agentTodoSourceInitial(todo()), '张')
})

test('real to-do sorting keeps urgent open work first and new completed work first', () => {
  const rows = [
    todo({
      todo_id: 'undated-new',
      due_at: null,
      created_at: '2026-10-02T02:00:00Z',
    }),
    todo({
      todo_id: 'due-late',
      due_at: '2026-10-05T08:00:00Z',
      created_at: '2026-10-01T01:00:00Z',
    }),
    todo({
      todo_id: 'due-soon',
      due_at: '2026-10-03T08:00:00Z',
      created_at: '2026-10-01T02:00:00Z',
    }),
    todo({
      todo_id: 'done-old',
      status: 'done',
      created_at: '2026-09-30T02:00:00Z',
    }),
    todo({
      todo_id: 'done-new',
      status: 'done',
      created_at: '2026-10-01T02:00:00Z',
    }),
  ]

  assert.deepEqual(
    sortAgentTodos(rows, 'open').map((item) => item.todo_id),
    ['due-soon', 'due-late', 'undated-new'],
  )
  assert.deepEqual(
    sortAgentTodos(rows, 'done').map((item) => item.todo_id),
    ['done-new', 'done-old'],
  )
})
