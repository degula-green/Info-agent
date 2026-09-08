import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  isHistoryStartAllowed,
  isPrivateConversation,
  discoveryAction,
  mapAttachmentStatus,
  mapCollectionStatus,
  pairingStatusLabel,
  oauthCallbackNotice,
  searchLoadedSources,
} from '../src/knowledge-mapping.ts'

test('maps connector conversation statuses without collapsing detached into missing', () => {
  assert.equal(mapCollectionStatus('active'), 'collecting')
  assert.equal(mapCollectionStatus('paused'), 'paused')
  assert.equal(mapCollectionStatus('detached'), 'detached')
  assert.equal(mapCollectionStatus('error'), 'error')
  assert.equal(mapCollectionStatus('unknown'), 'not_started')
})

test('maps pairing failure and attachment processing states', () => {
  assert.equal(pairingStatusLabel('pending'), '等待 Agent 配对')
  assert.equal(pairingStatusLabel('consumed'), '已完成配对')
  assert.equal(pairingStatusLabel('expired'), '已过期，请重新创建配对码')
  assert.equal(pairingStatusLabel('failed', 'wechat_path_invalid'), '本机微信数据库路径无效，请检查 Agent 配置')
  assert.equal(pairingStatusLabel('failed', 'other'), 'Agent 配对失败，请检查本机配置')
  assert.equal(mapAttachmentStatus('pending'), 'processing')
  assert.equal(mapAttachmentStatus('ready'), 'completed')
  assert.equal(mapAttachmentStatus('failed'), 'failed')
})

test('maps private conversations and OAuth callback results', () => {
  assert.equal(isPrivateConversation('private'), true)
  assert.equal(isPrivateConversation('group'), false)
  assert.deepEqual(oauthCallbackNotice({ connector: 'feishu', status: 'active' }), { kind: 'success', message: '飞书已绑定' })
  assert.deepEqual(oauthCallbackNotice({ connector: 'feishu', error: 'invalid_oauth_state' }), { kind: 'error', message: '飞书授权已失效，请重新发起授权' })
  assert.deepEqual(oauthCallbackNotice({ connector: 'wechat', status: 'active' }), null)
})

test('distinguishes new, supplemental, and already joined discoveries', () => {
  assert.equal(discoveryAction({}), 'attach')
  assert.equal(discoveryAction({ attachedConversationId: 'conversation-1' }), 'join')
  assert.equal(discoveryAction({ attachedConversationId: 'conversation-1', currentUserCollector: true }), 'attached')
})

test('enforces a seven-day history window in both directions', () => {
  const now = new Date('2026-09-05T12:00:00.000Z')
  assert.equal(isHistoryStartAllowed(new Date('2026-08-29T12:00:00.000Z'), now), true)
  assert.equal(isHistoryStartAllowed(new Date('2026-08-29T11:59:59.999Z'), now), false)
  assert.equal(isHistoryStartAllowed(new Date('2026-09-05T12:00:00.001Z'), now), false)
  assert.equal(isHistoryStartAllowed(undefined, now), true)
})

test('searches only loaded sources and respects the platform filter', () => {
  const sources = [
    {
      key: 'feishu',
      name: '飞书',
      chats: [{
        id: 'f-chat',
        name: 'release 发布群',
        members: 3,
        recentMessageTime: '刚刚',
        messages: [{ id: 'f-message', sender: 'A', content: 'release checklist', time: '刚刚' }],
        files: [],
      }],
    },
    {
      key: 'wechat',
      name: '个人微信',
      chats: [{
        id: 'w-chat',
        name: '家庭群',
        members: 4,
        recentMessageTime: '昨天',
        messages: [],
        files: [{ id: 'w-file', name: 'release-notes.txt', content: 'local file', uploader: 'B', time: '昨天', contentAccessRequired: true }],
      }],
    },
  ]

  const all = searchLoadedSources('release', 'all', sources)
  assert.deepEqual(all.map((item) => item.id), ['chat-f-chat', 'message-f-message', 'file-w-file'])
  assert.deepEqual(searchLoadedSources('release', 'feishu', sources).map((item) => item.id), ['chat-f-chat', 'message-f-message'])
  assert.deepEqual(searchLoadedSources('release', 'wecom', sources), [])
  assert.equal(all.find((item) => item.id === 'file-w-file')?.contentAccessRequired, true)
})
