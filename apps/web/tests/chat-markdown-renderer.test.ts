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

test('renders safe links and tables', () => {
  const html = renderChatMarkdown([
    '查看 [文档](https://example.com/report?q=1&lang=zh)。',
    '',
    '| 名称 | 状态 |',
    '| --- | --- |',
    '| 登录模块 | 已完成 |',
    '| 搜索模块 | 进行中 |',
  ].join('\n'))

  assert.equal(
    html.includes('<a href="https://example.com/report?q=1&amp;lang=zh" target="_blank" rel="noopener noreferrer">文档</a>'),
    true,
  )
  assert.equal(html.includes('<div class="chat-table-scroll"><table><thead><tr><th>名称</th><th>状态</th></tr></thead><tbody><tr><td>登录模块</td><td>已完成</td></tr><tr><td>搜索模块</td><td>进行中</td></tr></tbody></table></div>'), true)
})

test('does not render non-http markdown links', () => {
  const html = renderChatMarkdown('[危险链接](javascript:alert(1))')
  assert.equal(html.includes('<a '), false)
  assert.equal(html.includes('javascript:alert(1)'), true)
})
