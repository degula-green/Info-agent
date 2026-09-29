import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderChatMarkdown } from '../src/utils/chatMarkdownRenderer.ts'

test('renders answer paragraphs, headings, lists, formatting, and code blocks', () => {
  const html = renderChatMarkdown([
    '## 结论',
    '',
    '这是 **重点** 和 `code`。',
    '',
    '- 第一项',
    '- 第二项',
    '',
    '```ts',
    'const value = 1 < 2',
    '```',
  ].join('\n'))

  assert.equal(html.includes('<h2>结论</h2>'), true)
  assert.equal(html.includes('<strong>重点</strong>'), true)
  assert.equal(html.includes('<code>code</code>'), true)
  assert.equal(html.includes('<ul><li>第一项</li><li>第二项</li></ul>'), true)
  assert.equal(html.includes('<pre><code>const value = 1 &lt; 2</code></pre>'), true)
})

test('escapes untrusted HTML in answers', () => {
  const html = renderChatMarkdown('<script>alert("x")</script>')
  assert.equal(html.includes('<script>'), false)
  assert.equal(html.includes('&lt;script&gt;'), true)
})
