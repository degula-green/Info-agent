import { knowledgeRequest } from './http'

export interface ContactIdentityDTO { id: string; platform: string; external_user_id: string; display_name?: string; avatar_url?: string; mapped_user_id?: string; mapping_status: string }
export interface ContactDTO { id: string; kind: 'internal' | 'external'; internal_user_id?: string; display_name?: string; identities: ContactIdentityDTO[]; conversation_ids?: string[] }
export interface AvailableContactDTO { external_user_id: string; display_name?: string; avatar_url?: string; email?: string; department?: string; job_title?: string; selected: boolean }
export async function listContacts(platform = '') {
  const body = await knowledgeRequest<{ items: ContactDTO[] }>(`/contacts${platform ? `?platform=${encodeURIComponent(platform)}` : ''}`)
  return body.items || []
}
export async function discoverContacts(platform: 'wechat' | 'feishu', query = '') {
  const body = await knowledgeRequest<{ items: AvailableContactDTO[] }>(`/contacts/discover?platform=${encodeURIComponent(platform)}${query ? `&q=${encodeURIComponent(query)}` : ''}`)
  return body.items || []
}
export async function attachContact(input: { platform: 'wechat' | 'feishu'; externalUserID: string; displayName?: string; avatarURL?: string }) {
  return knowledgeRequest<ContactDTO>('/contacts', { method: 'POST', body: JSON.stringify({ platform: input.platform, external_user_id: input.externalUserID, display_name: input.displayName || '', avatar_url: input.avatarURL || '' }) })
}
export async function removeContact(id: string) { return knowledgeRequest<{ status: string }>(`/contacts/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
export async function getContact(id: string) { return knowledgeRequest<ContactDTO & { messages: unknown[]; attachments: unknown[] }>(`/contacts/${encodeURIComponent(id)}`) }
