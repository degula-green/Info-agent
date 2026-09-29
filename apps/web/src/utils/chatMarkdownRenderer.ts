function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function renderInline(value: string) {
  const code: string[] = []
  const tokenized = value.replace(/`([^`\n]+)`/g, (_, content: string) => {
    const index = code.push(`<code>${escapeHtml(content)}</code>`) - 1
    return `\u0000CODE${index}\u0000`
  })
  const escaped = escapeHtml(tokenized)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
  return escaped.replace(/\u0000CODE(\d+)\u0000/g, (_, index: string) => code[Number(index)] || '')
}

export function renderChatMarkdown(value: string) {
  const lines = String(value || '').replace(/\r\n?/g, '\n').split('\n')
  const output: string[] = []
  let paragraph: string[] = []
  let list: { type: 'ul' | 'ol'; items: string[] } | null = null
  let code: string[] | null = null

  const flushParagraph = () => {
    if (!paragraph.length) return
    output.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`)
    paragraph = []
  }
  const flushList = () => {
    if (!list) return
    output.push(`<${list.type}>${list.items.map((item) => `<li>${renderInline(item)}</li>`).join('')}</${list.type}>`)
    list = null
  }

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      flushParagraph()
      flushList()
      if (code == null) code = []
      else {
        output.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
        code = null
      }
      continue
    }
    if (code != null) {
      code.push(line)
      continue
    }

    const heading = /^(#{1,3})\s+(.+)$/.exec(line)
    const unordered = /^\s*[-*+]\s+(.+)$/.exec(line)
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line)
    const quote = /^\s*>\s?(.*)$/.exec(line)
    if (!line.trim()) {
      flushParagraph()
      flushList()
      continue
    }
    if (heading) {
      flushParagraph()
      flushList()
      output.push(`<h${heading[1].length}>${renderInline(heading[2])}</h${heading[1].length}>`)
      continue
    }
    if (unordered || ordered) {
      flushParagraph()
      const type = unordered ? 'ul' : 'ol'
      const activeList = list as { type: 'ul' | 'ol'; items: string[] } | null
      if (!activeList || activeList.type !== type) flushList()
      if (!list) list = { type, items: [] }
      const currentList = list as { type: 'ul' | 'ol'; items: string[] }
      currentList.items.push((unordered || ordered)?.[1] || '')
      continue
    }
    if (quote) {
      flushParagraph()
      flushList()
      output.push(`<blockquote>${renderInline(quote[1])}</blockquote>`)
      continue
    }
    flushList()
    paragraph.push(line)
  }
  if (code != null) output.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
  flushParagraph()
  flushList()
  return output.join('')
}
