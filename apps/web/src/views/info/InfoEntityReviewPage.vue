<template>
  <section class="entity-review-page">
    <header class="review-heading">
      <button class="review-heading__back" type="button" aria-label="返回我的组织" @click="router.push('/organization')">
        <t-icon name="chevron-left" />
      </button>
      <div class="review-heading__copy">
        <p>Entity review</p>
        <h1>候选实体审核</h1>
        <span>候选只有审核通过后才会进入正式实体树并参与检索。</span>
      </div>
      <div class="review-heading__actions">
        <t-button variant="outline" :loading="loading" @click="load">
          <template #icon><t-icon name="refresh" /></template>
          刷新
        </t-button>
      </div>
    </header>

    <t-alert v-if="error" theme="error" :message="error" :close="false" />

    <div class="review-toolbar">
      <t-input v-model="query" class="review-toolbar__search" clearable placeholder="搜索候选名称" @enter="load">
        <template #prefix-icon><t-icon name="search" /></template>
      </t-input>
      <t-select v-model="domain" class="review-toolbar__select" :options="domainOptions" placeholder="全部类型" clearable @change="load" />
      <t-select v-model="status" class="review-toolbar__select" :options="statusOptions" placeholder="全部状态" clearable @change="load" />
      <span class="review-toolbar__spacer" />
      <span class="review-toolbar__count">共 {{ total }} 条</span>
      <t-button variant="outline" :disabled="!selectedIds.length" @click="batchReview('promote')">批量通过 ({{ selectedIds.length }})</t-button>
      <t-button theme="danger" variant="outline" :disabled="!selectedIds.length" @click="batchReview('ignore')">批量忽略</t-button>
    </div>

    <t-table
      row-key="candidate_id"
      :data="items"
      :columns="columns"
      :loading="loading"
      :selected-row-keys="selectedIds"
      hover
      size="small"
      @select-change="onSelectChange"
      @row-click="(row: EntityCandidate) => open(row.candidate_id)"
    >
      <template #candidate_name="{ row }">
        <strong class="cell-name">{{ row.candidate_name }}</strong>
        <small class="cell-sub">{{ row.normalized_key }}</small>
      </template>
      <template #candidate_domain="{ row }">
        <t-tag variant="outline" size="small">{{ domainLabel(row.candidate_domain) }}</t-tag>
      </template>
      <template #score="{ row }">{{ Math.round((row.score || 0) * 100) }}%</template>
      <template #mention_count="{ row }">{{ row.mention_count }}</template>
      <template #status="{ row }">
        <t-tag :theme="statusTheme(row.status)" variant="light" size="small">{{ statusLabel(row.status) }}</t-tag>
      </template>
      <template #op="{ row }">
        <t-button size="small" variant="text" @click.stop="open(row.candidate_id)">详情</t-button>
      </template>
      <template #empty>
        <div class="review-empty">没有待处理的候选实体</div>
      </template>
    </t-table>

    <t-pagination
      v-if="total > pageSize"
      class="review-pagination"
      :current="page"
      :page-size="pageSize"
      :total="total"
      @current-change="onPageChange"
    />

    <t-drawer v-model:visible="drawerVisible" header="候选详情" size="520px" :footer="false" destroy-on-close>
      <div v-if="detail" class="review-detail">
        <div class="review-detail__hero">
          <div>
            <h3>{{ detail.candidate_name }}</h3>
            <p>{{ domainLabel(detail.candidate_domain) }} · {{ detail.normalized_key }}</p>
          </div>
          <t-tag :theme="statusTheme(detail.status)" variant="light">{{ statusLabel(detail.status) }}</t-tag>
        </div>

        <div class="review-detail__stats">
          <div><span>提取置信度</span><strong>{{ Math.round((detail.score || 0) * 100) }}%</strong></div>
          <div><span>提及次数</span><strong>{{ detail.mention_count }}</strong></div>
          <div><span>来源 Chunk</span><strong>{{ detail.distinct_chunk_count }}</strong></div>
          <div><span>涉及会话</span><strong>{{ detail.distinct_conversation_count }}</strong></div>
        </div>

        <section class="review-detail__section">
          <div class="review-detail__section-title">证据上下文</div>
          <div v-if="!detail.mentions.length" class="review-detail__muted">暂无证据</div>
          <blockquote v-for="mention in detail.mentions.slice(0, 5)" :key="mention.mention_id" class="review-evidence">
            <p>{{ mention.context_excerpt || mention.surface_form }}</p>
            <footer>{{ mention.extraction_method }} · 置信度 {{ Math.round((mention.confidence || 0) * 100) }}%</footer>
          </blockquote>
        </section>

        <section class="review-detail__section">
          <div class="review-detail__section-title">规范名称</div>
          <t-input v-model="draftName" placeholder="确认为正式实体时使用的名称" />
        </section>

        <section class="review-detail__section">
          <div class="review-detail__section-title">类型</div>
          <t-select v-model="draftDomain" :options="domainOptions.filter((item) => item.value)" />
        </section>

        <section v-if="mergeMode" class="review-detail__section">
          <div class="review-detail__section-title">合并到</div>
          <t-select
            v-model="mergeTarget"
            :options="entityOptions"
            filterable
            placeholder="选择目标实体"
          />
        </section>

        <section class="review-detail__section">
          <div class="review-detail__section-title">审核备注</div>
          <t-textarea v-model="note" placeholder="可选，用于后续质量分析" :autosize="{ minRows: 2, maxRows: 4 }" />
        </section>

        <div class="review-detail__actions">
          <t-button theme="primary" :loading="submitting" @click="submit('promote')">通过并创建实体</t-button>
          <t-button v-if="!mergeMode" variant="outline" @click="toggleMerge">合并到已有实体</t-button>
          <t-button v-else variant="outline" :loading="submitting" @click="submit('merge')">确认合并</t-button>
          <t-button variant="outline" :loading="submitting" @click="submit('defer')">暂缓</t-button>
          <t-button theme="danger" variant="text" :loading="submitting" @click="submit('ignore')">忽略</t-button>
        </div>
      </div>
    </t-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRouter } from 'vue-router'
import {
  getEntityCandidate,
  listAdminEntities,
  listEntityCandidates,
  reviewEntityCandidate,
  type AdminEntity,
  type EntityCandidate,
  type EntityCandidateDetail,
  type EntityReviewAction,
} from '../../api/rag.ts'
import { resolveOrganizationId } from '../../utils/info-search-scope.ts'

const router = useRouter()

const domainOptions = [
  { value: '', label: '全部类型' },
  { value: 'organization', label: '组织/公司' },
  { value: 'person', label: '人员' },
  { value: 'project', label: '项目' },
  { value: 'policy', label: '政策' },
  { value: 'contract', label: '合同' },
]

const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'new', label: '待处理' },
  { value: 'review_ready', label: '可审核' },
  { value: 'deferred', label: '已暂缓' },
]

const columns = [
  { colKey: 'row-select', type: 'multiple', width: 40 },
  { colKey: 'candidate_name', title: '候选名称', minWidth: 200, ellipsis: true },
  { colKey: 'candidate_domain', title: '类型', width: 110 },
  { colKey: 'score', title: '置信度', width: 90 },
  { colKey: 'mention_count', title: '提及', width: 70 },
  { colKey: 'status', title: '状态', width: 100 },
  { colKey: 'op', title: '', width: 70 },
]

const items = ref<EntityCandidate[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const query = ref('')
const domain = ref('')
const status = ref('')
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const selectedIds = ref<string[]>([])

const drawerVisible = ref(false)
// 审核停留时长的起点：抽屉打开那一刻。只有审核页知道这个值，
// 后端拿不到，所以由前端测量后随审核请求一起上报。
const openedAt = ref<number | null>(null)
const detail = ref<EntityCandidateDetail | null>(null)
const draftName = ref('')
const draftDomain = ref('')
const note = ref('')
const mergeMode = ref(false)
const mergeTarget = ref('')
const entities = ref<AdminEntity[]>([])

const entityOptions = computed(() =>
  entities.value
    .filter((item) => item.entity_id !== detail.value?.resolved_entity_id)
    .map((item) => ({ value: item.entity_id, label: `${item.canonical_name}（${domainLabel(item.domain)}）` })),
)

function domainLabel(value?: string | null) {
  const map: Record<string, string> = {
    organization: '组织/公司', person: '人员', project: '项目',
    policy: '政策', contract: '合同',
  }
  return map[String(value || '')] || String(value || '未分类')
}

function statusLabel(value?: string | null) {
  const map: Record<string, string> = {
    new: '待处理', review_ready: '可审核', grouped: '已归组',
    promoted: '已通过', merged: '已合并', ignored: '已忽略', deferred: '已暂缓',
  }
  return map[String(value || '')] || String(value || '')
}

function statusTheme(value?: string | null) {
  if (value === 'promoted' || value === 'merged') return 'success'
  if (value === 'ignored') return 'default'
  if (value === 'deferred') return 'warning'
  return 'primary'
}

async function organizationId() {
  const value = await resolveOrganizationId()
  if (!value) throw new Error('无法确定当前组织，请先加入组织')
  return value
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const scope = await organizationId()
    const response = await listEntityCandidates({
      organizationId: scope,
      status: status.value || undefined,
      domain: domain.value || undefined,
      query: query.value || undefined,
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = response.items
    total.value = response.total
    selectedIds.value = []
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载候选失败'
  } finally {
    loading.value = false
  }
}

function onSelectChange(value: Array<string | number>) {
  selectedIds.value = value.map((item) => String(item))
}

function onPageChange(value: { current: number; pageSize: number }) {
  page.value = value.current
  pageSize.value = value.pageSize
  load()
}

async function open(candidateId: string) {
  drawerVisible.value = true
  openedAt.value = Date.now()
  detail.value = null
  mergeMode.value = false
  mergeTarget.value = ''
  note.value = ''
  try {
    const scope = await organizationId()
    const value = await getEntityCandidate(candidateId, scope)
    detail.value = value
    draftName.value = value.candidate_name
    draftDomain.value = value.candidate_domain
    // A merge target can only be an existing entity, so the list is only
    // fetched when the reviewer actually reaches for merge.
    entities.value = []
    listAdminEntities(scope).then((response) => { entities.value = response.items }).catch(() => {})
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '加载候选详情失败'
    drawerVisible.value = false
    openedAt.value = null
  }
}

function toggleMerge() {
  mergeMode.value = !mergeMode.value
}

async function submit(action: EntityReviewAction) {
  if (!detail.value) return
  if (action === 'merge' && !mergeTarget.value) {
    MessagePlugin.warning('请选择要合并到的目标实体')
    return
  }
  submitting.value = true
  try {
    const scope = await organizationId()
    await reviewEntityCandidate({
      candidateId: detail.value.candidate_id,
      organizationId: scope,
      action,
      // A fresh idempotency key per submission: retrying the same review after
      // a network blip must not create a second entity.
      reviewRequestId: crypto.randomUUID(),
      canonicalName: action === 'promote' ? draftName.value : undefined,
      domain: action === 'promote' ? draftDomain.value : undefined,
      targetEntityId: action === 'merge' ? mergeTarget.value : undefined,
      note: note.value || undefined,
      expectedStatus: detail.value.status,
      // 停留时长只对"打开详情后作出决定"的路径有意义；批量审核从列表直接
      // 提交，没有停留过程，因此那条路径不发这个字段。
      durationMs: openedAt.value === null ? undefined : Date.now() - openedAt.value,
    })
    MessagePlugin.success(`已${actionLabel(action)}`)
    drawerVisible.value = false
    openedAt.value = null
    await load()
  } catch (exc) {
    MessagePlugin.error(exc instanceof Error ? exc.message : '审核提交失败')
  } finally {
    submitting.value = false
  }
}

function actionLabel(action: EntityReviewAction) {
  return { promote: '通过', merge: '合并', ignore: '忽略', defer: '暂缓' }[action]
}

async function batchReview(action: 'promote' | 'ignore') {
  if (!selectedIds.value.length) return
  submitting.value = true
  let succeeded = 0
  let failed = 0
  try {
    const scope = await organizationId()
    for (const candidateId of selectedIds.value) {
      try {
        await reviewEntityCandidate({
          candidateId, organizationId: scope, action,
          reviewRequestId: crypto.randomUUID(),
        })
        succeeded += 1
      } catch {
        failed += 1
      }
    }
  } finally {
    submitting.value = false
  }
  if (failed) MessagePlugin.warning(`成功 ${succeeded} 条，失败 ${failed} 条`)
  else MessagePlugin.success(`已处理 ${succeeded} 条`)
  await load()
}

onMounted(load)
</script>

<style scoped lang="less">
.entity-review-page { box-sizing: border-box; width: min(1180px, 100%); margin: 0 auto; padding: 30px 34px 58px; color: var(--td-text-color-primary); }
.review-heading { display: flex; align-items: flex-start; gap: 13px; margin-bottom: 18px; }
.review-heading__back { display: grid; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; margin-top: 2px; padding: 0; border: 1px solid var(--td-component-stroke); border-radius: 7px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); cursor: pointer; }
.review-heading__back:hover { color: var(--td-brand-color); border-color: var(--td-brand-color-4); }
.review-heading__copy { flex: 1; min-width: 0; }
.review-heading__copy p { margin: 0 0 5px; color: var(--td-brand-color); font-size: 11px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }
.review-heading__copy h1 { margin: 0; font-size: 26px; font-weight: 650; }
.review-heading__copy span { display: block; margin-top: 6px; color: var(--td-text-color-secondary); font-size: 13px; }
.review-heading__actions { display: flex; gap: 8px; }
.review-toolbar { display: flex; align-items: center; gap: 10px; margin: 16px 0 12px; }
.review-toolbar__search { width: 240px; }
.review-toolbar__select { width: 150px; }
.review-toolbar__spacer { flex: 1; }
.review-toolbar__count { color: var(--td-text-color-secondary); font-size: 13px; }
.cell-name { display: block; font-weight: 600; }
.cell-sub { display: block; margin-top: 2px; color: var(--td-text-color-placeholder); font-size: 12px; }
.review-empty { padding: 32px 0; text-align: center; color: var(--td-text-color-placeholder); }
.review-pagination { margin-top: 16px; justify-content: flex-end; }
.review-detail__hero { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.review-detail__hero h3 { margin: 0; font-size: 18px; font-weight: 650; }
.review-detail__hero p { margin: 4px 0 0; color: var(--td-text-color-secondary); font-size: 13px; }
.review-detail__stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.review-detail__stats > div { display: flex; align-items: baseline; justify-content: space-between; padding: 10px 12px; border: 1px solid var(--td-component-stroke); border-radius: var(--td-radius-medium); }
.review-detail__stats span { color: var(--td-text-color-secondary); font-size: 12px; }
.review-detail__section { margin-bottom: 16px; }
.review-detail__section-title { margin-bottom: 6px; font-size: 13px; font-weight: 600; }
.review-detail__muted { color: var(--td-text-color-placeholder); font-size: 13px; }
.review-evidence { margin: 0 0 8px; padding: 8px 10px; border-left: 3px solid var(--td-brand-color-3); background: var(--td-bg-color-secondarycontainer); }
.review-evidence p { margin: 0; font-size: 13px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
.review-evidence footer { margin-top: 4px; color: var(--td-text-color-placeholder); font-size: 12px; }
.review-detail__actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 20px; }
</style>
