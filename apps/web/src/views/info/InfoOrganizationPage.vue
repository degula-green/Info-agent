<template>
  <section class="organization-page">
    <header class="organization-heading">
      <div>
        <h1>我的组织</h1>
        <p>查看组织成员及其管理身份</p>
      </div>
      <div class="organization-heading__actions">
        <t-button v-if="organization && canLeave" variant="outline" theme="danger" @click="openExitDialog">
          <template #icon><t-icon name="logout" /></template>
          退出组织
        </t-button>
        <t-button variant="outline" @click="router.push('/organization/knowledge')">
          <template #icon><t-icon name="folder-open" /></template>
          知识结构
        </t-button>
        <t-button v-if="canInvite" theme="primary" :loading="pageLoading" @click="openInvitationDialog">
          <template #icon><t-icon name="user-add" /></template>
          生成邀请链接
        </t-button>
      </div>
    </header>

    <section class="organization-overview" aria-labelledby="organization-name">
      <div class="organization-identity">
        <span class="organization-logo"><t-icon name="usergroup" /></span>
        <div>
          <div class="organization-name-line">
          <h2 id="organization-name">{{ organization?.organization.name || '我的组织' }}</h2>
            <t-tag v-if="organization" theme="success" variant="light">{{ organizationStatusLabel }}</t-tag>
          </div>
          <p>组织标识：{{ organization?.organization.slug || '加载中...' }}</p>
        </div>
      </div>
      <dl class="organization-facts">
        <div><dt>组织成员</dt><dd>{{ members.length }}</dd></div>
        <div><dt>管理成员</dt><dd>{{ managementMemberCount }}</dd></div>
        <div><dt>我的身份</dt><dd>{{ currentIdentity }}</dd></div>
      </dl>
    </section>

    <section v-if="invitation" class="invitation-strip" :class="{ 'invitation-strip--revoked': invitation.status === 'revoked' }" aria-label="最近生成的邀请">
      <span class="invitation-strip__icon"><t-icon :name="invitation.status === 'pending' ? 'link' : 'close-circle'" /></span>
      <div class="invitation-strip__body">
        <strong>{{ invitation.status === 'pending' ? '邀请链接等待使用' : '邀请链接已撤销' }}</strong>
        <small>{{ invitation.status === 'pending' ? `有效期至 ${formatDateTime(invitation.expiresAt)}` : '此链接已失效，无法用于加入组织' }}</small>
      </div>
      <div v-if="invitation.status === 'pending'" class="invitation-strip__actions">
        <t-button size="small" variant="outline" @click="copyInvitation"><template #icon><t-icon name="file-copy" /></template>复制链接</t-button>
        <t-button size="small" theme="danger" variant="text" @click="revokeDialogVisible = true">撤销</t-button>
      </div>
    </section>

    <section v-if="canReviewAccess" class="access-review-section" aria-labelledby="access-review-title">
      <div class="members-toolbar">
        <div>
          <h2 id="access-review-title">原文访问申请</h2>
          <p>审批通过后立即同步原文查看或下载权限</p>
        </div>
      </div>
      <div v-if="accessLoading" class="member-loading"><t-loading size="small" text="正在加载访问申请..." /></div>
      <div v-else-if="accessRequests.length" class="access-review-list">
        <article class="access-review-row">
          <div>
            <strong>{{ accessRequests.length }} 条待审批申请</strong>
            <span>请在个人主页统一查看申请人、来源群聊和脱敏消息摘要</span>
          </div>
          <div class="access-review-actions">
            <t-button size="small" theme="primary" @click="router.push('/profile?tab=permissions')">进入审批中心</t-button>
          </div>
        </article>
      </div>
      <div v-else class="member-empty">
        <t-icon name="check-circle" />
        <strong>暂无待审批申请</strong>
        <span>新的原文查看或下载申请会出现在这里</span>
      </div>
    </section>

    <section class="members-section" aria-labelledby="members-title">
      <div class="members-toolbar">
        <div>
          <h2 id="members-title">成员</h2>
          <p>有效成员默认拥有基础成员权限</p>
        </div>
        <div class="members-filters">
          <t-input v-model="query" clearable placeholder="搜索昵称或邮箱" aria-label="搜索组织成员">
            <template #prefix-icon><t-icon name="search" /></template>
          </t-input>
          <select v-model="roleFilter" class="role-filter" aria-label="按角色筛选">
            <option value="all">全部身份</option>
            <option value="owner">管理员</option>
            <option value="information_admin">信息管理员</option>
            <option value="membership_approver">成员审批员</option>
            <option value="member">仅普通成员</option>
          </select>
        </div>
      </div>

      <div class="member-table" role="table" aria-label="组织成员列表">
        <div class="member-row member-row--header" role="row">
          <span role="columnheader">成员</span><span role="columnheader">身份</span><span role="columnheader">加入方式</span><span role="columnheader">加入时间</span><span aria-hidden="true"></span>
        </div>
        <div v-if="pageLoading" class="member-loading"><t-loading size="small" text="正在加载组织成员..." /></div>
        <div v-for="member in filteredMembers" v-else :key="member.id" class="member-row" role="row">
          <div class="member-person" role="cell">
            <span class="member-avatar" :style="{ background: member.color }">{{ member.nickname.slice(0, 1) }}</span>
            <div><strong>{{ member.nickname }}<small v-if="member.current">我</small></strong><span>{{ member.email }}</span></div>
          </div>
          <div class="member-roles" role="cell" data-label="身份">
            <t-tag v-for="role in visibleRoles(member)" :key="role" :theme="role === 'owner' ? 'success' : 'default'" variant="light">{{ roleLabel[role] }}</t-tag>
          </div>
          <span class="member-meta" role="cell" data-label="加入方式">{{ member.joined_via === 'created' ? '创建组织' : '邀请加入' }}</span>
          <span class="member-meta" role="cell" data-label="加入时间">{{ formatDate(member.joined_at) }}</span>
          <button v-if="canManageMembers || canManageRoles || canTransferOwner" class="member-more" type="button" title="管理成员" aria-label="管理成员" @click="openRoleDrawer(member)"><t-icon name="more" /></button>
        </div>
        <div v-if="!pageLoading && !filteredMembers.length" class="member-empty">
          <t-icon name="search" />
          <strong>未找到匹配成员</strong>
          <span>请尝试其他昵称、邮箱或身份条件</span>
        </div>
      </div>
    </section>

    <t-dialog v-model:visible="invitationDialogVisible" header="生成邀请链接" :footer="false" width="480px">
      <div class="invitation-dialog">
        <template v-if="!invitationDraft">
          <span class="dialog-mark"><t-icon name="user-add" /></span>
          <h3>邀请成员加入{{ organization?.organization.name || '组织' }}</h3>
          <p>邀请链接不绑定邮箱，任何获得链接的已登录用户均可接受邀请。链接将在 24 小时后失效。</p>
          <t-button theme="primary" block :loading="invitationSubmitting" @click="generateInvitation">生成邀请链接</t-button>
        </template>
        <template v-else>
          <span class="dialog-mark dialog-mark--success"><t-icon name="check" /></span>
          <h3>邀请链接已生成</h3>
          <p>链接仅在本次生成后展示，请妥善发送给受邀成员。</p>
          <div class="invitation-link"><span>{{ invitationDraft.url }}</span><button type="button" title="复制邀请链接" @click="copyInvitation"><t-icon name="file-copy" /></button></div>
          <small>有效期至 {{ formatDateTime(invitationDraft.expiresAt) }}</small>
          <div class="dialog-actions"><t-button variant="outline" @click="invitationDialogVisible = false">完成</t-button><t-button theme="primary" @click="copyInvitation"><template #icon><t-icon name="file-copy" /></template>复制链接</t-button></div>
        </template>
      </div>
    </t-dialog>

    <t-dialog v-model:visible="revokeDialogVisible" header="撤销邀请链接" width="420px" :confirm-btn="{ content: '确认撤销', theme: 'danger' }" cancel-btn="取消" @confirm="revokeInvitation">
      <p class="confirm-copy">撤销后该链接立即失效，尚未使用链接的用户将无法加入组织。</p>
    </t-dialog>

    <t-dialog v-model:visible="exitDialogVisible" header="退出组织" width="480px" @close="cancelExitCheck">
      <div class="exit-dialog">
        <div v-if="exitLoading" class="member-loading"><t-loading size="small" text="正在检查退出影响..." /></div>
        <template v-else-if="exitPreflight">
          <div v-if="(exitPreflight.blockers || []).length" class="exit-impact exit-impact--blocked">
            <strong>暂时无法退出</strong>
            <span v-for="item in (exitPreflight.blockers || [])" :key="item">{{ exitBlockerLabel(item) }}</span>
          </div>
          <div v-if="(exitPreflight.warnings || []).length" class="exit-impact">
            <strong>退出前请确认</strong>
            <span v-for="item in (exitPreflight.warnings || [])" :key="item">{{ exitWarningLabel(item) }}</span>
          </div>
        </template>
      </div>
      <template #footer>
        <div class="exit-dialog__footer">
          <t-button variant="outline" @click="cancelExitCheck">取消</t-button>
          <t-button v-if="exitPreflight" theme="danger" :disabled="!exitPreflight.allowed || exitSubmitting" :loading="exitSubmitting" @click="confirmLeave">确认退出</t-button>
        </div>
      </template>
    </t-dialog>

    <t-drawer v-model:visible="roleDrawerVisible" header="管理成员身份" size="420px" :footer="false" destroy-on-close>
      <div v-if="selectedMember" class="role-drawer">
        <div class="drawer-member">
          <span class="member-avatar" :style="{ background: selectedMember.color }">{{ selectedMember.nickname.slice(0, 1) }}</span>
          <div><strong>{{ selectedMember.nickname }}</strong><span>{{ selectedMember.email }}</span></div>
        </div>
        <div class="base-role"><span><strong>成员</strong><small>有效组织成员自动拥有基础成员权限</small></span><t-tag theme="success" variant="light">始终启用</t-tag></div>
        <div class="role-options">
          <label v-for="role in managementRoles" :key="role.code" class="role-option" :class="{ 'role-option--disabled': roleDisabled(role.code) }">
            <span><strong>{{ role.label }}</strong><small>{{ role.description }}</small><em v-if="roleDisabled(role.code)">组织必须至少保留一名管理员</em></span>
            <t-switch :model-value="selectedMember.roles.includes(role.code)" :disabled="roleDisabled(role.code) || roleSubmitting" @update:model-value="setRole(role.code, Boolean($event))" />
          </label>
        </div>
        <p class="drawer-note"><t-icon name="info-circle" />角色变更会立即同步到组织成员权限。</p>
        <div v-if="selectedMember && !selectedMember.current" class="member-actions">
          <t-button v-if="canManageMembers && selectedMember.status === 'active'" variant="outline" theme="warning" :loading="memberActionSubmitting" @click="suspendSelectedMember">暂停成员</t-button>
          <t-button v-if="canManageMembers && selectedMember.status === 'suspended'" variant="outline" theme="success" :loading="memberActionSubmitting" @click="reactivateSelectedMember">恢复成员</t-button>
          <t-button v-if="canTransferOwner && !selectedMember.roles.includes('owner')" variant="outline" theme="primary" :loading="memberActionSubmitting" @click="transferSelectedOwner">转让管理员</t-button>
          <t-button v-if="canManageMembers" variant="text" theme="danger" :loading="memberActionSubmitting" @click="removeSelectedMember">永久移除</t-button>
        </div>
      </div>
    </t-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRouter } from 'vue-router'
import { CoreAuthError, getCurrentUser } from '@/api/core-auth'
import { createOrganizationInvitation, getCurrentOrganization, getOrganizationCapabilities, getOrganizationExitPreflight, grantOrganizationRole, leaveOrganization, listAccessRequests, listOrganizationMembers, reactivateOrganizationMember, removeOrganizationMember, revokeOrganizationInvitation, revokeOrganizationRole, suspendOrganizationMember, transferOrganizationOwner, type CoreAccessRequest, type CoreOrganizationCapabilities, type CoreOrganizationExitPreflight, type CoreOrganizationMember, type CoreOrganizationResponse } from '@/api/core-organization'
import { desktopInvitationBaseUrl } from '@/api/runtime-config'

type ManagementRole = 'owner' | 'information_admin' | 'membership_approver'
type DisplayRole = ManagementRole | 'member'
type Member = CoreOrganizationMember & { id: string; color: string; current?: boolean }
type Invitation = { id: string; url: string; expiresAt: Date; status: 'pending' | 'revoked' }

const roleLabel: Record<DisplayRole, string> = { owner: '管理员', information_admin: '信息管理员', membership_approver: '成员审批员', member: '成员' }
const router = useRouter()
const managementRoles: Array<{ code: ManagementRole; label: string; description: string }> = [
  { code: 'owner', label: '管理员', description: '拥有组织全部管理权限，包括角色管理' },
  { code: 'information_admin', label: '信息管理员', description: '管理组织信息资源，不包含成员准入权限' },
  { code: 'membership_approver', label: '成员审批员', description: '创建和撤销邀请，不包含信息管理权限' },
]
const organization = ref<CoreOrganizationResponse | null>(null)
const capabilities = ref<CoreOrganizationCapabilities | null>(null)
const members = ref<Member[]>([])
const pageLoading = ref(true)
const invitationSubmitting = ref(false)
const roleSubmitting = ref(false)
const currentUserID = ref('')
const query = ref('')
const roleFilter = ref<DisplayRole | 'all'>('all')
const roleDrawerVisible = ref(false)
const selectedMemberID = ref('')
const invitationDialogVisible = ref(false)
const revokeDialogVisible = ref(false)
const invitation = ref<Invitation | null>(null)
const invitationDraft = ref<Invitation | null>(null)
const accessRequests = ref<CoreAccessRequest[]>([])
const accessLoading = ref(false)
const exitDialogVisible = ref(false)
const exitLoading = ref(false)
const exitSubmitting = ref(false)
const exitPreflight = ref<CoreOrganizationExitPreflight | null>(null)
const memberActionSubmitting = ref(false)
let exitAbortController: AbortController | null = null
let exitAbortTimer: number | null = null
let exitCheckTimedOut = false

const selectedMember = computed(() => members.value.find((item) => item.id === selectedMemberID.value) || null)
const managementMemberCount = computed(() => members.value.filter((item) => item.roles.length > 0).length)
const currentRoles = computed(() => organization.value?.membership.roles.map((role) => role.role_code || role.RoleCode || '').filter(Boolean) || [])
const canManageRoles = computed(() => capabilities.value?.can_manage_roles ?? currentRoles.value.includes('owner'))
const canManageMembers = computed(() => capabilities.value?.can_manage_members ?? false)
const canTransferOwner = computed(() => capabilities.value?.can_transfer_owner ?? false)
const canLeave = computed(() => capabilities.value?.can_leave ?? false)
const canInvite = computed(() => capabilities.value?.can_invite ?? (currentRoles.value.includes('owner') || currentRoles.value.includes('membership_approver')))
const canReviewAccess = computed(() => currentRoles.value.includes('owner') || currentRoles.value.includes('information_admin'))
const currentIdentity = computed(() => currentRoles.value.includes('owner') ? '管理员' : currentRoles.value.includes('information_admin') ? '信息管理员' : currentRoles.value.includes('membership_approver') ? '成员审批员' : '成员')
const organizationStatusLabel = computed(() => organization.value?.organization.status === 'active' ? '正常' : organization.value?.organization.status || '')
const filteredMembers = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase()
  return members.value.filter((member) => {
    const matchesQuery = !needle || `${member.nickname} ${member.email}`.toLocaleLowerCase().includes(needle)
    const matchesRole = roleFilter.value === 'all' || (roleFilter.value === 'member' ? member.roles.length === 0 : member.roles.includes(roleFilter.value))
    return matchesQuery && matchesRole
  })
})

function visibleRoles(member: Member): DisplayRole[] { return member.roles.length ? member.roles.map((role) => role as ManagementRole) : ['member'] }
function formatDate(value: string) { return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value)) }
function formatDateTime(value: Date) { return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(value) }
function openRoleDrawer(member: Member) { selectedMemberID.value = member.id; roleDrawerVisible.value = true }
function exitBlockerLabel(value: string) {
  if (value === 'LAST_OWNER_REQUIRED') return '你是最后一个管理员，请先转让管理员身份。'
  if (value === 'ACTIVE_COLLECTOR_RESPONSIBILITY') return '你仍是部分群聊的唯一有效采集者，请先暂停、解除或转让采集任务。'
  return value
}
function exitWarningLabel(value: string) {
  const labels: Record<string, string> = {
    'organization knowledge and question history will become unavailable': '退出后将无法查看原组织知识、附件和组织问答。',
    'documents already downloaded cannot be revoked': '已经下载的内容无法撤回。',
    'organization collector assignments will be removed': '你的组织采集任务会被移除。',
  }
  return labels[value] || value
}
function isAbortError(cause: any) {
  return cause?.name === 'AbortError' || cause?.code === 'ABORT_ERR'
}
function clearExitRequest() {
  if (exitAbortTimer !== null) {
    window.clearTimeout(exitAbortTimer)
    exitAbortTimer = null
  }
  exitAbortController = null
}
function cancelExitCheck() {
  exitCheckTimedOut = false
  clearExitRequest()
  exitLoading.value = false
  exitPreflight.value = null
  exitDialogVisible.value = false
}
async function openExitDialog() {
  if (!organization.value) return
  cancelExitCheck()
  exitDialogVisible.value = true
  exitLoading.value = true
  exitPreflight.value = null
  exitCheckTimedOut = false
  const controller = new AbortController()
  exitAbortController = controller
  exitAbortTimer = window.setTimeout(() => {
    exitCheckTimedOut = true
    controller.abort()
  }, 8000)
  try {
    exitPreflight.value = await getOrganizationExitPreflight(organization.value.organization.id, controller.signal)
  } catch (cause) {
    if (isAbortError(cause)) {
      if (exitCheckTimedOut && exitDialogVisible.value) MessagePlugin.error('退出影响检查超时，请稍后重试')
    } else {
      MessagePlugin.error(errorMessage(cause, '退出影响检查失败'))
    }
  } finally {
    if (exitAbortController === controller) {
      clearExitRequest()
      exitLoading.value = false
    }
  }
}
async function confirmLeave() {
  if (!organization.value || !exitPreflight.value?.allowed || exitSubmitting.value) return
  exitSubmitting.value = true
  try {
    await leaveOrganization(organization.value.organization.id)
    MessagePlugin.success('已退出组织')
    exitDialogVisible.value = false
    organization.value = null
    capabilities.value = null
    members.value = []
    await router.push('/profile')
  } catch (cause) {
    MessagePlugin.error(errorMessage(cause, '退出组织失败'))
  } finally {
    exitSubmitting.value = false
  }
}
function roleDisabled(role: ManagementRole) { return role === 'owner' && Boolean(selectedMember.value?.roles.includes('owner')) && members.value.filter((item) => item.roles.includes('owner')).length === 1 }
async function setRole(role: ManagementRole, enabled: boolean) {
  const member = selectedMember.value
  if (!member || roleDisabled(role)) return
  if (!organization.value) return
  roleSubmitting.value = true
  try {
    if (enabled) await grantOrganizationRole(organization.value.organization.id, member.user_id, role)
    else await revokeOrganizationRole(organization.value.organization.id, member.user_id, role)
    await loadMembers()
    MessagePlugin.success(`${roleLabel[role]}已${enabled ? '授予' : '撤销'}`)
  } catch (cause) { MessagePlugin.error(errorMessage(cause, '角色更新失败')) }
  finally { roleSubmitting.value = false }
}
function openInvitationDialog() { invitationDraft.value = null; invitationDialogVisible.value = true }
async function generateInvitation() {
  if (!organization.value || invitationSubmitting.value) return
  invitationSubmitting.value = true
  try {
    const result = await createOrganizationInvitation(organization.value.organization.id)
    const invitationBaseURL = desktopInvitationBaseUrl()
    const inviteURL = invitationBaseURL
      ? `${invitationBaseURL}#token=${encodeURIComponent(result.token)}`
      : `${window.location.origin}/organization-invitations/${result.token}`
    const generated = { id: result.invitation_id, url: inviteURL, expiresAt: new Date(result.expires_at), status: 'pending' as const }
    invitationDraft.value = generated; invitation.value = generated
  } catch (cause) { MessagePlugin.error(errorMessage(cause, '邀请链接生成失败')) }
  finally { invitationSubmitting.value = false }
}
async function copyInvitation() {
  const current = invitationDraft.value || invitation.value
  if (!current || current.status !== 'pending') return
  try { await navigator.clipboard.writeText(current.url); MessagePlugin.success('邀请链接已复制') }
  catch { MessagePlugin.warning('复制失败，请手动选择链接') }
}
async function revokeInvitation() {
  if (!organization.value || !invitation.value) return
  try {
    await revokeOrganizationInvitation(organization.value.organization.id, invitation.value.id)
    invitation.value.status = 'revoked'; if (invitationDraft.value) invitationDraft.value.status = 'revoked'
    revokeDialogVisible.value = false; invitationDialogVisible.value = false
    MessagePlugin.success('邀请链接已撤销')
  } catch (cause) { MessagePlugin.error(errorMessage(cause, '邀请撤销失败')) }
}
function errorMessage(cause: any, fallback: string) { return cause?.message || fallback }
function memberColor(id: string) { const colors = ['#07a85b', '#2f6fed', '#8b5cf6', '#d97706', '#0891b2', '#64748b']; return colors[Math.abs([...id].reduce((sum, char) => sum + char.charCodeAt(0), 0)) % colors.length] }
async function loadMembers() {
  if (!organization.value) return
  const result = await listOrganizationMembers(organization.value.organization.id)
  members.value = result.members.map((member) => ({ ...member, id: member.user_id, color: memberColor(member.user_id), current: member.user_id === currentUserID.value }))
}
async function loadCapabilities() {
  if (!organization.value) return
  capabilities.value = await getOrganizationCapabilities(organization.value.organization.id)
}
async function loadAccessRequests() {
  if (!organization.value || !canReviewAccess.value) {
    accessRequests.value = []
    return
  }
  accessLoading.value = true
  try {
    const result = await listAccessRequests('review', organization.value.organization.id)
    accessRequests.value = result.items || []
  } catch (cause) {
    MessagePlugin.error(errorMessage(cause, '访问申请加载失败'))
  } finally {
    accessLoading.value = false
  }
}
async function loadOrganization() {
  pageLoading.value = true
  try {
    const [current, user] = await Promise.all([getCurrentOrganization(), getCurrentUser()])
    organization.value = current; currentUserID.value = user.id; await Promise.all([loadMembers(), loadCapabilities()]); await loadAccessRequests()
  } catch (cause) {
    if (cause instanceof CoreAuthError && cause.code === 'ORG_MEMBERSHIP_REQUIRED') MessagePlugin.info('你尚未加入组织')
    else MessagePlugin.error(errorMessage(cause, '组织信息加载失败'))
  } finally { pageLoading.value = false }
}
async function runMemberAction(action: () => Promise<void>, success: string) {
  if (!organization.value || memberActionSubmitting.value) return
  memberActionSubmitting.value = true
  try {
    await action()
    await Promise.all([loadMembers(), loadCapabilities()])
    MessagePlugin.success(success)
    roleDrawerVisible.value = false
  } catch (cause) {
    MessagePlugin.error(errorMessage(cause, '成员操作失败'))
  } finally {
    memberActionSubmitting.value = false
  }
}
function suspendSelectedMember() {
  const member = selectedMember.value
  if (!member || !organization.value || !window.confirm(`确认暂停 ${member.nickname}？`)) return
  void runMemberAction(() => suspendOrganizationMember(organization.value!.organization.id, member.user_id), '成员已暂停')
}
function reactivateSelectedMember() {
  const member = selectedMember.value
  if (!member || !organization.value) return
  void runMemberAction(() => reactivateOrganizationMember(organization.value!.organization.id, member.user_id), '成员已恢复')
}
function transferSelectedOwner() {
  const member = selectedMember.value
  if (!member || !organization.value || !window.confirm(`确认将管理员身份转让给 ${member.nickname}？`)) return
  void runMemberAction(() => transferOrganizationOwner(organization.value!.organization.id, member.user_id), '管理员身份已转让')
}
function removeSelectedMember() {
  const member = selectedMember.value
  if (!member || !organization.value || !window.confirm(`永久移除 ${member.nickname}？该操作不可撤销。`)) return
  void runMemberAction(() => removeOrganizationMember(organization.value!.organization.id, member.user_id), '成员已移除')
}
onMounted(() => { void loadOrganization() })
onBeforeUnmount(cancelExitCheck)
</script>

<style lang="less" scoped>
.organization-page { box-sizing: border-box; width: min(1100px, 100%); margin: 0 auto; padding: 34px 34px 56px; color: var(--td-text-color-primary); }
.organization-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
.organization-heading__actions { display: flex; align-items: center; gap: 9px; }
.organization-heading__actions :deep(.t-button) { min-height: 32px; }
.organization-heading__actions :deep(.t-button__text) { display: inline-flex; align-items: center; line-height: 1; }
.organization-heading h1 { margin: 0; font-size: 28px; font-weight: 650; }
.organization-heading p { margin: 7px 0 0; color: var(--td-text-color-secondary); font-size: 13px; }
.organization-overview { display: flex; align-items: center; justify-content: space-between; gap: 32px; padding: 24px 28px; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.organization-identity { display: flex; align-items: center; gap: 15px; min-width: 0; }
.organization-logo { display: grid; place-items: center; flex: 0 0 50px; width: 50px; height: 50px; border-radius: 8px; color: #fff; background: var(--td-brand-color); font-size: 24px; }
.organization-name-line { display: flex; align-items: center; gap: 9px; min-width: 0; }
.organization-name-line h2 { overflow: hidden; margin: 0; font-size: 18px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.organization-identity p { margin: 6px 0 0; color: var(--td-text-color-placeholder); font-size: 12px; }
.organization-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0; }
.organization-facts div { padding: 0 22px; border-left: 1px solid var(--td-component-stroke); }
.organization-facts dt { color: var(--td-text-color-secondary); font-size: 11px; }
.organization-facts dd { margin: 6px 0 0; font-size: 16px; font-weight: 650; }
.invitation-strip { display: flex; align-items: center; gap: 12px; margin-top: 16px; padding: 13px 15px; border: 1px solid var(--td-brand-color-3); border-radius: 7px; background: var(--td-brand-color-1); }
.invitation-strip--revoked { border-color: var(--td-component-stroke); background: var(--td-bg-color-secondarycontainer); }
.invitation-strip__icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 6px; color: var(--td-brand-color); background: var(--td-bg-color-container); }
.invitation-strip--revoked .invitation-strip__icon { color: var(--td-text-color-placeholder); }
.invitation-strip__body { display: grid; flex: 1; gap: 3px; min-width: 0; }
.invitation-strip__body strong { font-size: 13px; }.invitation-strip__body small { color: var(--td-text-color-secondary); font-size: 11px; }
.invitation-strip__actions { display: flex; align-items: center; gap: 5px; }
.members-section { margin-top: 28px; }
.access-review-section { margin-top: 28px; }
.access-review-list { display: grid; overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.access-review-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 18px; padding: 14px 16px; border-bottom: 1px solid var(--td-component-stroke); }
.access-review-row:last-child { border-bottom: 0; }
.access-review-row > div:first-child { display: grid; min-width: 0; gap: 3px; }
.access-review-row strong, .access-review-row span, .access-review-row small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.access-review-row strong { font-size: 13px; }
.access-review-row span, .access-review-row small, .access-review-row time { color: var(--td-text-color-secondary); font-size: 12px; }
.access-review-actions { display: flex; gap: 8px; }
.members-toolbar { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 14px; }
.members-toolbar h2 { margin: 0; font-size: 19px; font-weight: 650; }.members-toolbar p { margin: 5px 0 0; color: var(--td-text-color-secondary); font-size: 12px; }
.members-filters { display: flex; align-items: center; gap: 9px; }.members-filters .t-input { width: 240px; }
.role-filter { width: 138px; height: 32px; padding: 0 28px 0 10px; border: 1px solid var(--td-component-stroke); border-radius: 4px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); font: inherit; font-size: 12px; }
.member-table { overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.member-row { display: grid; grid-template-columns: minmax(210px, 1.45fr) minmax(220px, 1.2fr) 105px 112px 36px; align-items: center; min-height: 68px; gap: 14px; padding: 11px 16px; border-top: 1px solid var(--td-component-stroke); }
.member-row--header { min-height: 42px; border-top: 0; color: var(--td-text-color-placeholder); background: var(--td-bg-color-secondarycontainer); font-size: 11px; }
.member-person { display: flex; align-items: center; gap: 11px; min-width: 0; }.member-person > div { min-width: 0; }.member-person strong, .member-person span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.member-person strong { font-size: 13px; font-weight: 600; }.member-person strong small { display: inline; margin-left: 6px; padding: 1px 5px; border-radius: 3px; color: var(--td-brand-color); background: var(--td-brand-color-1); font-size: 10px; font-weight: 500; }
.member-person > div > span, .member-meta { margin-top: 4px; color: var(--td-text-color-secondary); font-size: 11px; }
.member-avatar { display: grid !important; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; border-radius: 50%; color: #fff; font-size: 12px; font-weight: 600; }
.member-roles { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.member-more { display: grid; place-items: center; width: 30px; height: 30px; padding: 0; border: 0; border-radius: 5px; color: var(--td-text-color-secondary); background: transparent; cursor: pointer; }.member-more:hover { color: var(--td-text-color-primary); background: var(--td-bg-color-container-hover); }
.member-empty { display: grid; place-items: center; min-height: 210px; padding: 30px; color: var(--td-text-color-placeholder); text-align: center; }.member-empty svg { width: 28px; height: 28px; }.member-empty strong { margin-top: 10px; color: var(--td-text-color-secondary); font-size: 13px; }.member-empty span { margin-top: 5px; font-size: 11px; }
.invitation-dialog { text-align: center; }.dialog-mark { display: grid; place-items: center; width: 48px; height: 48px; margin: 2px auto 14px; border-radius: 8px; color: var(--td-brand-color); background: var(--td-brand-color-1); font-size: 22px; }.dialog-mark--success { color: #fff; background: var(--td-brand-color); }
.invitation-dialog h3 { margin: 0; font-size: 16px; }.invitation-dialog p { margin: 8px auto 18px; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.7; }.invitation-dialog > small { display: block; margin-top: 8px; color: var(--td-text-color-placeholder); font-size: 11px; text-align: left; }
.invitation-link { display: flex; align-items: center; gap: 8px; padding: 9px 9px 9px 11px; border: 1px solid var(--td-component-stroke); border-radius: 6px; background: var(--td-bg-color-secondarycontainer); text-align: left; }.invitation-link span { flex: 1; overflow: hidden; color: var(--td-text-color-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.invitation-link button { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 4px; color: var(--td-brand-color); background: var(--td-bg-color-container); cursor: pointer; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }.confirm-copy { margin: 0; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.7; }
.role-drawer { padding-bottom: 20px; }.drawer-member { display: flex; align-items: center; gap: 11px; padding-bottom: 18px; border-bottom: 1px solid var(--td-component-stroke); }.drawer-member div { display: grid; gap: 4px; }.drawer-member strong { font-size: 14px; }.drawer-member div span { color: var(--td-text-color-secondary); font-size: 11px; }
.base-role, .role-option { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 16px 2px; border-bottom: 1px solid var(--td-component-stroke); }.base-role > span, .role-option > span { display: grid; gap: 4px; }.base-role strong, .role-option strong { font-size: 13px; }.base-role small, .role-option small { color: var(--td-text-color-secondary); font-size: 11px; line-height: 1.5; }.role-option { cursor: pointer; }.role-option--disabled { cursor: not-allowed; }.role-option em { color: var(--td-warning-color); font-size: 10px; font-style: normal; }
.drawer-note { display: flex; align-items: flex-start; gap: 7px; margin: 18px 0 0; padding: 11px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 11px; line-height: 1.55; }.drawer-note svg { flex: 0 0 15px; width: 15px; margin-top: 1px; color: var(--td-brand-color); }
.member-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--td-component-stroke); }
.exit-dialog { display: grid; gap: 10px; min-height: 56px; }.exit-dialog__footer { display: flex; justify-content: flex-end; gap: 8px; }.exit-impact { display: grid; gap: 6px; padding: 12px 13px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 12px; line-height: 1.6; }.exit-impact + .exit-impact { margin-top: 0; }.exit-impact strong { color: var(--td-text-color-primary); font-size: 13px; }.exit-impact--blocked { border-color: var(--td-error-color-3); color: var(--td-error-color-7); background: var(--td-error-color-1); }
@media (max-width: 900px) { .organization-overview { align-items: flex-start; flex-direction: column; }.organization-facts { width: 100%; }.organization-facts div:first-child { border-left: 0; padding-left: 0; }.members-toolbar { align-items: stretch; flex-direction: column; }.members-filters .t-input { flex: 1; width: auto; }.member-row { grid-template-columns: minmax(190px, 1.3fr) minmax(185px, 1fr) 100px 36px; }.member-row > :nth-child(4) { display: none; } }
@media (max-width: 700px) { .organization-page { padding: 24px 16px 44px; }.organization-heading { align-items: stretch; flex-direction: column; }.organization-heading .t-button { align-self: flex-start; }.organization-overview { padding: 18px; }.organization-facts div { padding: 0 12px; }.organization-facts dd { font-size: 14px; }.invitation-strip { align-items: flex-start; flex-wrap: wrap; }.invitation-strip__actions { width: 100%; padding-left: 44px; }.members-filters { align-items: stretch; flex-direction: column; }.role-filter { width: 100%; }.member-table { border-radius: 7px; }.member-row--header { display: none; }.member-row { grid-template-columns: 1fr auto; gap: 11px 14px; padding: 15px; }.member-person { grid-column: 1; }.member-more { grid-column: 2; grid-row: 1; }.member-roles { grid-column: 1 / -1; }.member-meta { grid-column: 1 / -1; margin: 0; }.member-meta::before { content: attr(data-label) '：'; color: var(--td-text-color-placeholder); }.member-row > :nth-child(4) { display: block; }.access-review-row { grid-template-columns: 1fr; align-items: start; }.access-review-actions { justify-content: flex-end; }.dialog-actions { flex-direction: row; }.dialog-actions .t-button { flex: 1; } }
</style>
