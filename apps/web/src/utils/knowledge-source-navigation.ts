import type { RouteLocationNormalizedLoaded, Router } from 'vue-router'
import { getConversation } from '@/api/info-knowledge'

const CONNECTOR_PLATFORMS = new Set(['feishu', 'wecom', 'wechat'])

export type KnowledgeSourceTarget = {
  platform?: string | null
  conversationId?: string | number | null
  messageId?: string | number | null
  attachmentId?: string | number | null
}

function cleanReturnPath(router: Router, route: RouteLocationNormalizedLoaded): string {
  const query: Record<string, string | string[]> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (key === 'return' || key === 'message' || key === 'attachment' || value == null) continue
    query[key] = Array.isArray(value) ? value.map(String) : String(value)
  }
  return router.resolve({ path: route.path, query }).fullPath
}

export async function navigateToKnowledgeSource(
  router: Router,
  route: RouteLocationNormalizedLoaded,
  target: KnowledgeSourceTarget,
): Promise<void> {
  const conversationID = String(target.conversationId || '').trim()
  if (!conversationID) throw new Error('missing source conversation')

  let platform = String(target.platform || '').trim()
  if (!CONNECTOR_PLATFORMS.has(platform)) {
    platform = String((await getConversation(conversationID)).platform || '').trim()
  }
  if (!CONNECTOR_PLATFORMS.has(platform)) throw new Error('missing source platform')

  const query: Record<string, string> = { return: cleanReturnPath(router, route) }
  if (target.messageId) query.message = String(target.messageId)
  if (target.attachmentId) query.attachment = String(target.attachmentId)
  await router.push({
    path: `/knowledge/${encodeURIComponent(platform)}/conversations/${encodeURIComponent(conversationID)}`,
    query,
  })
}
