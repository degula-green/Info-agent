import { getCurrentOrganization } from '@/api/core-organization'
import type { InfoChat } from '@/mock'
import { useInfoKnowledgeStore } from '@/stores/infoKnowledge'

let organizationIdPromise: Promise<string | undefined> | null = null
let organizationIdResolved: string | undefined | null = null

export function resolveOrganizationId() {
  // Cache successes only. A transient Core failure must not pin organizationId
  // to undefined for the rest of the page session (search then loses org scope).
  if (organizationIdResolved) return Promise.resolve(organizationIdResolved)
  if (!organizationIdPromise) {
    organizationIdPromise = getCurrentOrganization()
      .then((value) => {
        organizationIdResolved = value.organization.id
        return organizationIdResolved
      })
      .catch(() => {
        organizationIdPromise = null
        return undefined
      })
  }
  return organizationIdPromise
}

/** Physical ES knowledge_base_id values from already-attached conversations. */
export async function resolveGlobalSearchScope() {
  const store = useInfoKnowledgeStore()
  const organizationId = await resolveOrganizationId()
  // Avoid reloading the connector directory on every keystroke; only warm it once.
  if (!store.loadedAt) {
    try { await store.ensureSources() } catch { /* search can still proceed with org scope */ }
  }
  return { organizationId, knowledgeBaseIds: store.collectKnowledgeBaseIds() }
}

/**
 * Logical library ids (e.g. organization:groups:{org}) are directory keys, not
 * ES knowledge_base_id values. Resolve physical UUIDs from attached chats, or
 * fall back to org-scoped global search for organization libraries.
 */
export async function resolveLibrarySearchScope(
  libraryKind: string,
  items: Array<{ conversation_id?: string; platform?: string }> = [],
) {
  const store = useInfoKnowledgeStore()
  const organizationId = await resolveOrganizationId()
  // Personal libraries are stored in the user partition; the RAG search
  // defaults to organization scope, so the scope has to be stated explicitly.
  const scopeType: 'organization' | 'user' = libraryKind.startsWith('private_') ? 'user' : 'organization'
  if (!store.loadedAt) {
    try { await store.ensureSources() } catch { /* fall through to org/global scope */ }
  }
  // A file library aggregates uploads, and an upload is not attached to a
  // conversation. Scoping the search to the attached chats' knowledge bases
  // therefore excluded every file the user had just uploaded, so an archived
  // weekly report could not be found by its content.
  if (libraryKind === 'organization_files' || libraryKind === 'private_local') {
    return { organizationId, knowledgeBaseIds: [], mode: 'global' as const, scopeType }
  }
  const fromItems = new Set<string>()
  for (const item of items) {
    if (!item.conversation_id) continue
    const chat =
      store.findConversation(item.platform || '', item.conversation_id) ||
      store.allChats.find((entry) => String(entry.id) === String(item.conversation_id))
    if (chat?.knowledgeBaseId) fromItems.add(String(chat.knowledgeBaseId))
  }
  if (fromItems.size) {
    return { organizationId, knowledgeBaseIds: [...fromItems], mode: 'knowledge' as const, scopeType }
  }

  const filter = (chat: InfoChat) => {
    if (libraryKind === 'organization_conversation') return !chat.isDirect
    if (libraryKind === 'private_conversation') return Boolean(chat.isDirect)
    return true
  }
  const knowledgeBaseIds = store.collectKnowledgeBaseIds(filter)
  if (knowledgeBaseIds.length) {
    return { organizationId, knowledgeBaseIds, mode: 'knowledge' as const, scopeType }
  }

  // Never pass the logical library.id into RAG; org libraries can still search
  // by organization_id, personal libraries by owner-visible scope.
  return { organizationId, knowledgeBaseIds: [], mode: 'global' as const, scopeType }
}

/** Explain empty search when ES found candidates but authz dropped them all. */
export function searchEmptyHint(diagnostics?: Record<string, any> | null): string {
  const candidates = Number(diagnostics?.candidate_count || 0)
  const authorized = Number(diagnostics?.authorized_count || 0)
  if (candidates > 0 && authorized === 0) {
    return '检索到相关内容，但当前账号无权查看。请确认已加入对应组织，或稍后重试。'
  }
  return '试试更短的关键词，或搜索群聊名称、发送人和文件名'
}
