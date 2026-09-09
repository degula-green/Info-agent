import { CoreAuthError, getAccessToken, refresh } from './core-auth'

export interface CoreOrganization { id: string; name: string; slug: string; status: string; created_at: string }
export interface CoreMembership { id: string; user_id: string; status: string; joined_via: string; roles: Array<{ role_code?: string; RoleCode?: string; granted_at?: string; GrantedAt?: string }> }
export interface CoreOrganizationResponse { organization: CoreOrganization; membership: CoreMembership }
export interface CoreOrganizationMember {
  membership_id: string
  user_id: string
  email: string
  nickname: string
  status: string
  joined_via: string
  joined_at: string
  roles: string[]
}
export interface CoreOrganizationMembersResponse { members: CoreOrganizationMember[] }
export interface CoreInvitationResponse { invitation_id: string; organization_id: string; token: string; expires_at: string }

const env = ((import.meta as ImportMeta & { env?: Record<string, string> }).env || {})
const baseURL = String(env.VITE_CORE_BASE_URL || '/api/core').replace(/\/$/, '')
function requestID() { return globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}` }

async function request<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json'); headers.set('Content-Type', 'application/json')
  headers.set('X-Request-ID', requestID()); headers.set('X-Trace-ID', requestID())
  const token = getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${baseURL}${path}`, { ...init, credentials: 'include', headers })
  const raw = await response.text(); let body: any = null
  if (raw) { try { body = JSON.parse(raw) } catch { body = raw } }
  if (response.status === 401 && !retried) { try { await refresh(); return request<T>(path, init, true) } catch { /* use original error */ } }
  if (!response.ok) { const error = body && typeof body === 'object' ? body : {}; throw new CoreAuthError(error.message || `Core request failed (${response.status})`, error.code || 'request_failed', response.status, Boolean(error.retryable)) }
  return body as T
}

export const getCurrentOrganization = () => request<CoreOrganizationResponse>('/organizations/current')
export const listOrganizationMembers = (organizationID: string) => request<CoreOrganizationMembersResponse>(`/organizations/${encodeURIComponent(organizationID)}/members`)
export const createOrganizationInvitation = (organizationID: string) => request<CoreInvitationResponse>(`/organizations/${encodeURIComponent(organizationID)}/invitations`, { method: 'POST', body: '{}' })
export const revokeOrganizationInvitation = (organizationID: string, invitationID: string) => request<void>(`/organizations/${encodeURIComponent(organizationID)}/invitations/${encodeURIComponent(invitationID)}/revoke`, { method: 'POST' })
export const grantOrganizationRole = (organizationID: string, userID: string, roleCode: string) => request<void>(`/organizations/${encodeURIComponent(organizationID)}/members/${encodeURIComponent(userID)}/roles`, { method: 'POST', body: JSON.stringify({ role_code: roleCode }) })
export const revokeOrganizationRole = (organizationID: string, userID: string, roleCode: string) => request<void>(`/organizations/${encodeURIComponent(organizationID)}/members/${encodeURIComponent(userID)}/roles/${encodeURIComponent(roleCode)}`, { method: 'DELETE' })
export const createOrganization = (name: string) => request<CoreOrganizationResponse>('/organizations', { method: 'POST', body: JSON.stringify({ name }) })
