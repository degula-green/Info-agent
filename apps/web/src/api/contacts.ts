import { knowledgeRequest } from './http'

export interface ContactIdentityDTO { id: string; platform: string; external_user_id: string; display_name?: string; avatar_url?: string; mapped_user_id?: string; mapping_status: string }
export interface ContactDTO { id: string; kind: 'internal' | 'external'; internal_user_id?: string; display_name?: string; identities: ContactIdentityDTO[]; conversation_ids?: string[] }
export async function listContacts(platform = '') {
  const body = await knowledgeRequest<{ items: ContactDTO[] }>(`/contacts${platform ? `?platform=${encodeURIComponent(platform)}` : ''}`)
  return body.items || []
}
