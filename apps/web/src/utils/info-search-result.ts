import type { SearchResult, SourceKey } from '@/mock'
import type { RagSearchItem } from '@/api/rag'

const PLATFORMS = new Set(['feishu', 'wecom', 'wechat'])

function text(value: unknown): string {
  return value == null ? '' : String(value).trim()
}

function platformOf(item: Record<string, any>): SourceKey | 'all' {
  const raw = text(item.platform || item.source_platform).toLowerCase()
  return PLATFORMS.has(raw) ? (raw as SourceKey) : 'all'
}

function platformLabel(platform: SourceKey | 'all'): string {
  return platform === 'feishu' ? '飞书' : platform === 'wecom' ? '企业微信' : platform === 'wechat' ? '个人微信' : '知识库'
}

function stripHighlight(value: string): string {
  return value.replace(/<\/?em>/gi, '').replace(/\s+/g, ' ').trim()
}

function truncate(value: string, max = 120): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  if (normalized.length <= max) return normalized
  return `${normalized.slice(0, max - 1)}…`
}

function formatTime(value: unknown): string | undefined {
  const raw = text(value)
  if (!raw) return undefined
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  return date.toLocaleString('zh-CN')
}

function kindOf(item: Record<string, any>): SearchResult['kind'] {
  const partKind = text(item.part_kind).toLowerCase()
  if (partKind === 'attachment_content' || partKind === 'attachment_metadata') return 'file'
  if (partKind === 'message_display' || partKind === 'knowledge_original') return 'message'
  if (item.attachment_id || item.file_name || partKind.includes('attachment')) return 'file'
  if (item.message_id || item.conversation_group_id || item.conversation_id || item.conversation_key) return 'message'
  return 'message'
}

/** Normalize mock or already-mapped UI search results. */
export function mapInfoSearchResult(item: SearchResult | Record<string, any>): SearchResult {
  const value = item as SearchResult
  return {
    ...value,
    source: value.source || String((item as any).platform || ''),
    platform: value.platform || 'all',
    excerpt: value.excerpt || value.content || value.title,
    score: value.score ?? 0.9,
    conversationId: value.conversationId || value.chatId,
  }
}

/** Map a RAG chunk hit into the frontend SearchResult shape used by Palette / drawers. */
export function mapRagSearchItem(item: RagSearchItem): SearchResult {
  const platform = platformOf(item)
  const kind = kindOf(item)
  const highlight = stripHighlight(text(item.highlight))
  const content = text(item.content)
  const fileName = text(item.file_name)
  const sender = text(item.sender_display_name || item.sender_name || item.uploader)
  const conversation = text(item.conversation_name || item.conversation_group_id || item.conversation_id || item.conversation_key)
  const time = formatTime(item.sent_at || item.observed_at || item.collected_at)
  const chatId = text(item.conversation_group_id || item.conversation_id || item.conversation_key) || undefined
  const recordId = text(kind === 'file' ? item.attachment_id : item.message_id || item.attachment_id) || undefined
  const title = kind === 'file'
    ? (fileName || truncate(highlight || content) || '附件')
    : (truncate(highlight || content) || fileName || '消息')
  const subtitleParts = [platformLabel(platform), conversation || undefined, sender || undefined, time].filter(Boolean)
  return {
    id: text(item.chunk_id) || `${kind}-${recordId || title}`,
    kind,
    title,
    subtitle: subtitleParts.join(' · '),
    source: platformLabel(platform),
    platform,
    chatId,
    recordId,
    content: content || highlight || title,
    excerpt: highlight || content || title,
    sender: sender || undefined,
    uploader: kind === 'file' ? (sender || undefined) : undefined,
    time,
    score: typeof item.score === 'number' ? item.score : Number(item.score) || 0,
    conversationId: chatId,
    contentAccessRequired: Boolean(item.content_access_required),
  }
}

export function mapRagSearchItems(items: RagSearchItem[] | undefined | null): SearchResult[] {
  return (items || []).map(mapRagSearchItem)
}

export function isAbortError(error: unknown): boolean {
  return Boolean(error && typeof error === 'object' && (error as any).name === 'AbortError')
}
