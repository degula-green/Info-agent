<template>
  <section class="access-center-page">
    <header class="access-center-heading">
      <div>
        <h1>权限与审批</h1>
        <p>{{ organization ? `${organization.organization.name} 的查看、下载和删除申请统一在这里处理` : '查看和处理权限申请' }}</p>
      </div>
      <t-button variant="outline" :loading="pageLoading" @click="loadPage">刷新</t-button>
    </header>

    <t-alert v-if="!pageLoading && !organization" theme="warning" message="你尚未加入组织，组织范围内的权限申请不可用。" :close="false" />

    <section class="access-center-panel" aria-labelledby="access-center-title">
      <div class="access-center-panel__heading">
        <div>
          <h2 id="access-center-title">申请记录</h2>
          <p>{{ accessEmptyText }}</p>
        </div>
        <span v-if="pendingReviewCount" class="pending-count">{{ pendingReviewCount }} 条待处理</span>
      </div>

      <t-tabs v-model="accessScope" @change="handleAccessScopeChange">
        <t-tab-panel value="inbox" label="待我处理" />
        <t-tab-panel value="mine" label="我发起的" />
        <t-tab-panel value="history" label="历史记录" />
      </t-tabs>

      <div class="access-toolbar">
        <t-radio-group v-model="typeFilter" variant="default-filled" size="small">
          <t-radio-button value="all">全部</t-radio-button>
          <t-radio-button value="view">查看</t-radio-button>
          <t-radio-button value="download">下载</t-radio-button>
          <t-radio-button value="delete">删除</t-radio-button>
        </t-radio-group>
      </div>

      <div class="access-request-list">
        <div v-if="accessLoading || pageLoading" class="access-request-state"><t-icon name="loading" />正在加载权限申请…</div>
        <div v-else-if="!filteredAccessRequests.length" class="access-request-state">{{ accessEmptyText }}</div>
        <article v-for="request in filteredAccessRequests" v-else :key="request.id" class="access-request">
          <div class="access-request__main">
            <div class="access-request__title">
              <strong>{{ request.title }}</strong>
              <t-tag variant="outline">{{ accessKindLabel(request.kind) }}</t-tag>
            </div>
            <small>{{ request.subtitle }}</small>
            <small v-if="request.excerpt" class="access-request__excerpt">{{ request.excerpt }}</small>
            <small v-if="request.deletionError" class="access-request__error">删除未完成：{{ request.deletionError }}</small>
            <small>{{ request.reason || '未填写申请说明' }} · {{ formatDateTime(request.created_at) }}</small>
          </div>
          <t-tag :theme="request.statusTheme" variant="light">{{ request.statusLabel }}</t-tag>
          <div v-if="request.canReview || request.canRetry" class="access-request__actions">
            <t-button v-if="request.source === 'core' && request.contextAvailable" size="small" variant="text" @click="openAccessContext(request)">查看上下文</t-button>
            <t-button v-if="request.canRetry" size="small" variant="outline" :loading="retrying[request.id]" @click="retryDeletion(request)">重试删除</t-button>
            <template v-if="request.canReview">
              <t-button size="small" theme="primary" @click="reviewAccess(request, 'approve')">通过</t-button>
              <t-button size="small" theme="danger" variant="outline" @click="reviewAccess(request, 'reject')">拒绝</t-button>
            </template>
          </div>
        </article>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import { getCurrentUser } from '@/api/core-auth'
import {
  approveAccessRequest,
  getCurrentOrganization,
  listAccessRequests,
  rejectAccessRequest,
  type CoreAccessRequest,
  type CoreOrganizationResponse,
} from '@/api/core-organization'
import {
  approveDeletionRequest,
  approvePrivateAccessRequest,
  listDeletionRequests,
  listPrivateAccessRequests,
  rejectDeletionRequest,
  rejectPrivateAccessRequest,
  retryDeletionRequest,
  type DeletionRequestDTO,
  type PrivateAccessRequestDTO,
} from '@/api/info-knowledge'
import {
  accessStatusLabel,
  accessStatusTheme,
  deletionHasError,
  deletionStatusLabel,
  deletionStatusTheme,
  matchesAccessView,
  matchesDeletionView,
  type AccessStatusTheme,
  type AccessView,
} from '@/access-control/access-display'

type AccessKind = 'view' | 'download' | 'delete'
type AccessTypeFilter = 'all' | AccessKind
type UnifiedAccessRequest = {
  id: string
  source: 'core' | 'private' | 'deletion'
  kind: AccessKind
  title: string
  subtitle: string
  excerpt: string
  reason: string
  status: string
  statusLabel: string
  statusTheme: AccessStatusTheme
  created_at: string
  contextAvailable: boolean
  canReview: boolean
  canRetry: boolean
  deletionError: string
  raw: CoreAccessRequest | PrivateAccessRequestDTO | DeletionRequestDTO
}

const route = useRoute()
const router = useRouter()
const organization = ref<CoreOrganizationResponse | null>(null)
const currentUserID = ref('')
const pageLoading = ref(true)
const accessLoading = ref(false)
const accessScope = ref<AccessView>(normalizeAccessView(route.query.tab))
const typeFilter = ref<AccessTypeFilter>('all')
const accessRequests = ref<UnifiedAccessRequest[]>([])
const retrying = reactive<Record<string, boolean>>({})
let requestSequence = 0

const filteredAccessRequests = computed(() => (
  typeFilter.value === 'all'
    ? accessRequests.value
    : accessRequests.value.filter((item) => item.kind === typeFilter.value)
))
const pendingReviewCount = computed(() => accessRequests.value.filter((item) => item.canReview).length)
const accessEmptyText = computed(() => {
  if (accessScope.value === 'inbox') return '当前没有需要你处理的申请'
  if (accessScope.value === 'history') return '暂无历史申请记录'
  return '暂无进行中的申请'
})

function normalizeAccessView(value: unknown): AccessView {
  return value === 'mine' || value === 'history' || value === 'inbox' ? value : 'inbox'
}

function accessKindLabel(kind: AccessKind) {
  return ({ view: '查看', download: '下载', delete: '删除' } as Record<AccessKind, string>)[kind]
}

function errorMessage(cause: any, fallback: string) {
  return cause?.message || cause?.error?.message || fallback
}

async function loadPage() {
  pageLoading.value = true
  try {
    const [currentOrganization, user] = await Promise.all([
      getCurrentOrganization().catch(() => null),
      getCurrentUser().catch(() => null),
    ])
    organization.value = currentOrganization
    currentUserID.value = String(user?.id || '')
    await loadAccessRequests()
  } catch (cause) {
    MessagePlugin.error(errorMessage(cause, '权限申请加载失败'))
  } finally {
    pageLoading.value = false
  }
}

async function handleAccessScopeChange(value: string | number) {
  accessScope.value = normalizeAccessView(value)
  await router.replace({ query: { ...route.query, tab: accessScope.value } })
  await loadAccessRequests()
}

async function loadAccessRequests() {
  const sequence = ++requestSequence
  accessLoading.value = true
  try {
    const organizationID = organization.value?.organization.id || ''
    const sourceScope = accessScope.value === 'inbox' ? 'inbox' : 'mine'
    const deletionView = accessScope.value === 'history' ? 'history' : 'active'
    const [privateItems, coreItems, deletionItems] = await Promise.all([
      listPrivateAccessRequests(sourceScope).catch(() => [] as PrivateAccessRequestDTO[]),
      organizationID
        ? listAccessRequests(sourceScope === 'inbox' ? 'review' : 'mine', organizationID)
            .then((result) => result.items || [])
            .catch(() => [] as CoreAccessRequest[])
        : Promise.resolve([] as CoreAccessRequest[]),
      listDeletionRequests(sourceScope, deletionView).catch(() => [] as DeletionRequestDTO[]),
    ])
    const unified = [
      ...privateItems
        .filter((item) => matchesAccessView(item.status, accessScope.value))
        .map((item) => unifiedPrivateAccessRequest(item, accessScope.value)),
      ...coreItems
        .filter((item) => matchesAccessView(item.status, accessScope.value))
        .map((item) => unifiedCoreAccessRequest(item, accessScope.value)),
      ...deletionItems
        .filter((item) => matchesDeletionView(item.status, accessScope.value))
        .map((item) => unifiedDeletionRequest(item, accessScope.value)),
    ].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at))
    if (sequence === requestSequence) accessRequests.value = unified
  } catch (cause) {
    if (sequence === requestSequence) accessRequests.value = []
    MessagePlugin.error(errorMessage(cause, '权限申请加载失败'))
  } finally {
    if (sequence === requestSequence) accessLoading.value = false
  }
}

async function reviewAccess(request: UnifiedAccessRequest, action: 'approve' | 'reject') {
  try {
    if (request.source === 'deletion') {
      if (action === 'approve') await approveDeletionRequest(request.id)
      else await rejectDeletionRequest(request.id)
    } else if (request.source === 'core') {
      if (action === 'approve') await approveAccessRequest(request.id)
      else await rejectAccessRequest(request.id)
    } else if (action === 'approve') {
      await approvePrivateAccessRequest(request.id)
    } else {
      await rejectPrivateAccessRequest(request.id)
    }
    await loadAccessRequests()
    MessagePlugin.success(action === 'approve' ? '已通过权限申请' : '已拒绝权限申请')
  } catch (cause) {
    MessagePlugin.error(errorMessage(cause, action === 'approve' ? '审批失败' : '拒绝失败'))
  }
}

async function retryDeletion(request: UnifiedAccessRequest) {
  if (request.source !== 'deletion') return
  retrying[request.id] = true
  try {
    await retryDeletionRequest(request.id)
    MessagePlugin.success('已重新提交删除')
    await loadAccessRequests()
  } catch (cause) {
    MessagePlugin.error(errorMessage(cause, '重试删除失败'))
  } finally {
    retrying[request.id] = false
  }
}

function unifiedPrivateAccessRequest(request: PrivateAccessRequestDTO, view: AccessView): UnifiedAccessRequest {
  const kind: AccessKind = request.requested_action === 'download' ? 'download' : 'view'
  const resource = request.resource_type === 'attachment' ? '附件' : '私聊信息'
  return {
    id: request.id,
    source: 'private',
    kind,
    title: `${resource} · ${accessKindLabel(kind)}`,
    subtitle: '私聊共享权限',
    excerpt: '',
    reason: request.reason || '',
    status: request.status,
    statusLabel: accessStatusLabel(request.status),
    statusTheme: accessStatusTheme(request.status),
    created_at: request.created_at,
    contextAvailable: false,
    canReview: view === 'inbox' && request.status === 'pending',
    canRetry: false,
    deletionError: '',
    raw: request,
  }
}

function unifiedCoreAccessRequest(request: CoreAccessRequest, view: AccessView): UnifiedAccessRequest {
  const requester = request.requester_nickname || request.requester_email || request.requester_user_id
  const kind: AccessKind = request.action === 'download' ? 'download' : 'view'
  const resource = request.resource_type === 'attachment_content' ? (kind === 'download' ? '附件下载' : '附件查看') : '消息原文查看'
  const source = [platformLabel(request.source_platform), request.source_conversation_name].filter(Boolean).join(' · ')
  const sender = request.sender_display_name ? `发送人 ${request.sender_display_name}` : ''
  const sent = request.sent_at ? formatDateTime(request.sent_at) : ''
  return {
    id: request.id,
    source: 'core',
    kind,
    title: `${requester} 申请${resource}`,
    subtitle: [source, sender, sent].filter(Boolean).join(' · ') || '来源上下文不可用',
    excerpt: request.file_name || request.masked_excerpt || '',
    reason: request.reason || '',
    status: request.status,
    statusLabel: accessStatusLabel(request.status),
    statusTheme: accessStatusTheme(request.status),
    created_at: request.created_at,
    contextAvailable: Boolean(request.source_conversation_id),
    canReview: view === 'inbox' && request.status === 'pending',
    canRetry: false,
    deletionError: '',
    raw: request,
  }
}

function unifiedDeletionRequest(request: DeletionRequestDTO, view: AccessView): UnifiedAccessRequest {
  const requester = request.requester_display_name || request.requester_email ||
    (request.requester_user_id === currentUserID.value ? '我' : `用户 ${shortID(request.requester_user_id)}`)
  const targetError = request.targets.find((target) => target.last_error)?.last_error || ''
  const hasError = deletionHasError(request.status, Boolean(request.last_error || targetError))
  return {
    id: request.id,
    source: 'deletion',
    kind: 'delete',
    title: `${requester} 申请删除消息`,
    subtitle: request.target_summary || `消息 ${shortID(request.scope_id)}`,
    excerpt: '',
    reason: request.reason || '',
    status: request.status,
    statusLabel: deletionStatusLabel(request.status),
    statusTheme: deletionStatusTheme(request.status),
    created_at: request.created_at,
    contextAvailable: false,
    canReview: view === 'inbox' && request.status === 'pending' && request.can_review,
    canRetry: hasError && request.status !== 'completed' && request.can_review,
    deletionError: hasError ? request.last_error || targetError || '部分内容删除失败' : '',
    raw: request,
  }
}

function shortID(value?: string) {
  const text = String(value || '').trim()
  return text.length > 8 ? `${text.slice(0, 8)}…` : text || '未知资源'
}

function platformLabel(value?: string) {
  return value === 'feishu' ? '飞书' : value === 'wechat' ? '个人微信' : value === 'wecom' ? '企业微信' : value || ''
}

function openAccessContext(request: UnifiedAccessRequest) {
  if (request.source !== 'core') return
  const raw = request.raw as CoreAccessRequest
  if (!raw.source_conversation_id || !raw.source_platform) return
  const query = new URLSearchParams({ return: '/organization/permissions' })
  if (raw.source_message_id) query.set('message', raw.source_message_id)
  void router.push(`/knowledge/${encodeURIComponent(raw.source_platform)}/conversations/${encodeURIComponent(raw.source_conversation_id)}?${query}`)
}

function formatDateTime(value?: string) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' })
}

onMounted(() => { void loadPage() })
</script>

<style lang="less" scoped>
.access-center-page {
  width: min(980px, 100%);
  margin: 0 auto;
  padding: 34px 34px 56px;
  color: var(--td-text-color-primary);
}

.access-center-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 22px;
}

.access-center-heading h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 650;
}

.access-center-heading p {
  margin: 7px 0 0;
  color: var(--td-text-color-secondary);
  font-size: 13px;
}

.access-center-panel {
  overflow: hidden;
  border: 1px solid var(--td-component-stroke);
  border-radius: 8px;
  background: var(--td-bg-color-container);
}

.access-center-panel__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 20px 24px 16px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.access-center-panel__heading h2 {
  margin: 0;
  font-size: 17px;
  font-weight: 650;
}

.access-center-panel__heading p {
  margin: 5px 0 0;
  color: var(--td-text-color-secondary);
  font-size: 12px;
}

.pending-count {
  flex: 0 0 auto;
  padding: 4px 9px;
  border-radius: 999px;
  color: var(--td-brand-color);
  background: var(--td-brand-color-1);
  font-size: 12px;
}

.access-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--td-component-stroke);
  background: var(--td-bg-color-secondarycontainer);
}

.access-request-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 180px;
  color: var(--td-text-color-secondary);
  font-size: 13px;
}

.access-request {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 78px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.access-request:last-child {
  border-bottom: 0;
}

.access-request__main {
  flex: 1;
  min-width: 0;
}

.access-request__title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.access-request__main strong,
.access-request__main small {
  display: block;
}

.access-request__main strong {
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.access-request__main small {
  margin-top: 5px;
  overflow: hidden;
  color: var(--td-text-color-secondary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.access-request__main .access-request__excerpt {
  color: var(--td-text-color-primary);
  font-weight: 500;
}

.access-request__main .access-request__error {
  color: var(--td-error-color-7);
}

.access-request__actions {
  display: flex;
  flex: 0 0 auto;
  gap: 8px;
}

@media (max-width: 760px) {
  .access-center-page {
    padding: 24px 16px 44px;
  }

  .access-center-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 14px;
  }

  .access-center-panel__heading {
    align-items: flex-start;
  }

  .access-toolbar {
    align-items: flex-start;
    overflow-x: auto;
  }

  .access-request {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .access-request__main {
    flex-basis: 100%;
  }

  .access-request__actions {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
