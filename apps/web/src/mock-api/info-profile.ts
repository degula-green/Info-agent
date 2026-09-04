import { useInfoMockStore } from '@/stores/infoMock'
import type { InfoProfile, SourceKey } from '@/mock'

export type ConnectorPlatform = SourceKey
export type Profile = { id: number; username: string; nickname: string; email: string; avatar_url: string | null; updated_at: string }
export type Connector = { platform: ConnectorPlatform; display_name: string; bound: boolean; status: string; availability: 'available' | 'unavailable'; account_name: string; cleanup_pending: boolean; last_error?: string | null }
function profileValue(): Profile { const store = useInfoMockStore(); return { id: 1, username: store.profile.email.split('@')[0], nickname: store.profile.nickname, email: store.profile.email, avatar_url: null, updated_at: '' } }
export async function getProfile() { return profileValue() }
export async function updateProfile(nickname: string) { const store = useInfoMockStore(); store.updateProfile({ nickname }); return profileValue() }
export async function uploadAvatar(_file: File) { return profileValue() }
export async function removeAvatar() { return profileValue() }
export async function getFeishuAuthorizeURL(_intent: 'bind' | 'rebind') { return '#' }
export async function bindWechat(_body: { wxid: string; db_dir: string }, _rebind = false) { useInfoMockStore().bindSource('wechat'); return profileValue() }
export async function unbindConnector(platform: ConnectorPlatform) { useInfoMockStore().unbindSource(platform); return profileValue() }
export async function getConnectors(): Promise<Connector[]> { const store = useInfoMockStore(); return store.sources.map((source) => ({ platform: source.key, display_name: source.name, bound: source.bound, status: source.bound ? 'active' : 'unbound', availability: source.key === 'wecom' ? 'unavailable' : 'available', account_name: source.account, cleanup_pending: false })) }
