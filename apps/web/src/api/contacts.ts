import { knowledgeRequest } from './http.ts'
import type { AttachmentDTO } from './info-knowledge.ts'

export interface ContactIdentityDTO { id: string; platform: string; external_user_id: string; display_name?: string; avatar_url?: string; mapped_user_id?: string; mapping_status: string }
export interface ContactDTO { id: string; kind: 'internal' | 'external'; internal_user_id?: string; display_name?: string; identities: ContactIdentityDTO[]; conversation_ids?: string[]; message_count?: number; attachment_count?: number }
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
export interface ContactAccessDTO { status: 'granted' | 'locked' | 'requested' | string; share_reference_id?: string; resource_type?: 'message' | 'attachment' | string; resource_id?: string; requested_action?: 'view' | 'download' | string }
export interface ContactFactDTO { id: string; message_id?: string; conversation_id: string; sender_identity_id?: string; fact_type: 'phone' | 'email' | 'id_card' | 'bank_card' | 'secret' | 'db_config' | string; label: string; raw_value?: string; occurred_at: string; created_at: string; access: ContactAccessDTO }
export interface ContactProfileDTO { id?: string; owner_user_id?: string; contact_key: string; summary: string; source_fingerprint?: string; status: 'pending' | 'ready' | 'failed' | string; last_error?: string; generated_at?: string | null; updated_at?: string }
export interface ContactAttachmentDTO extends AttachmentDTO { access: ContactAccessDTO; content_access: ContactAccessDTO; download_access: ContactAccessDTO }
export interface ContactDetailDTO { contact: ContactDTO; profile: ContactProfileDTO; facts: ContactFactDTO[]; attachments: ContactAttachmentDTO[] }
export async function getContact(id: string) { return knowledgeRequest<ContactDetailDTO>(`/contacts/${encodeURIComponent(id)}`) }
export async function refreshContactProfile(id: string) { return knowledgeRequest<ContactProfileDTO>(`/contacts/${encodeURIComponent(id)}/profile/refresh`, { method: 'POST' }) }
