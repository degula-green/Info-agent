import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mapRagSearchItems } from '../src/utils/info-search-result.ts'

test('keeps one display result per source resource while retaining message hits', () => {
  const results = mapRagSearchItems([
    {
      chunk_id: 'message-1',
      resource_type: 'message',
      resource_id: 'message-record-1',
      message_id: 'message-record-1',
      source_conversation_id: 'conversation-1',
      content: '青云小组马上招新了',
      score: 0.9,
    },
    {
      chunk_id: 'attachment-chunk-1',
      resource_type: 'attachment',
      resource_id: 'attachment-1',
      document_id: 'attachment-1',
      message_id: 'attachment-message-1',
      source_conversation_id: 'conversation-1',
      file_name: '常见问题.docx',
      content: '第一段',
      score: 0.8,
    },
    {
      chunk_id: 'attachment-chunk-2',
      resource_type: 'attachment',
      resource_id: 'attachment-1',
      document_id: 'attachment-1',
      message_id: 'attachment-message-1',
      source_conversation_id: 'conversation-1',
      file_name: '常见问题.docx',
      content: '第二段',
      score: 0.7,
    },
  ])

  assert.equal(results.length, 2)
  assert.deepEqual(results.map((item) => item.kind), ['message', 'file'])
  assert.equal(results[0]?.chatId, 'conversation-1')
  assert.equal(results[0]?.messageId, 'message-record-1')
  assert.equal(results[1]?.conversationId, 'conversation-1')
  assert.equal(results[1]?.messageId, 'attachment-message-1')
  assert.equal(results[1]?.recordId, 'attachment-1')
})
