function stripProviderSenderPrefix(value: string) {
  const colon = value.indexOf(':')
  if (colon <= 0 || /[\s\r\n]/.test(value.slice(0, colon))) return value
  const prefix = value.slice(0, colon).toLowerCase()
  if (prefix.startsWith('wxid_') || prefix.startsWith('gh_') || prefix.endsWith('@chatroom')) return value.slice(colon + 1).trim()
  return value
}

export function isDisplayableTextMessage(messageType: string | undefined, content: string | undefined | null) {
  const normalizedType = String(messageType || '').trim().toLowerCase()
  if (normalizedType === 'system') return false
  const value = stripProviderSenderPrefix(String(content || '').trim())
  if (!value) return false
  const lower = value.toLowerCase()
  if (lower.startsWith('<?xml') || lower.startsWith('<msg') || lower.startsWith('<appmsg')) return false
  if (value.startsWith('{') && value.endsWith('}')) {
    try {
      JSON.parse(value)
      return false
    } catch {
      // Keep malformed plain text visible; only valid structured payloads are filtered.
    }
  }
  switch (value.toLowerCase()) {
    case 'merged and forwarded message':
    case 'forwarded message':
    case 'file name':
    case 'filename':
    case '[无法解析]':
    case '[表情]':
    case '[动画表情]':
    case '<msg>':
      return false
    default:
      return true
  }
}
