<template>
  <section v-if="collapsed" class="sidebar-schedules sidebar-schedules--collapsed">
    <button
      class="sidebar-schedules__collapsed"
      type="button"
      :title="collapsedTitle"
      aria-label="日程"
      @click="requestExpand"
    >
      <t-icon name="calendar" />
      <span v-if="pendingCount" class="sidebar-schedules__badge">{{ pendingCount > 9 ? '9+' : pendingCount }}</span>
    </button>
  </section>

  <section v-else class="sidebar-schedules" aria-label="日程">
    <button class="sidebar-schedules__head" type="button" @click="listOpen = !listOpen">
      <t-icon name="calendar" class="sidebar-schedules__head-icon" />
      <span class="sidebar-schedules__head-title">日程</span>
      <span v-if="pendingCount" class="sidebar-schedules__count">{{ pendingCount }}</span>
      <t-icon :name="listOpen ? 'chevron-down' : 'chevron-right'" class="sidebar-schedules__head-chevron" />
    </button>

    <div v-if="listOpen" class="sidebar-schedules__body">
      <p v-if="loadError" class="sidebar-schedules__hint sidebar-schedules__hint--error">{{ loadError }}</p>
      <p v-else-if="!visibleDrafts.length" class="sidebar-schedules__hint">暂无待确认日程</p>

      <ul v-else class="sidebar-schedules__list">
        <li
          v-for="draft in visibleDrafts"
          :key="draft.taskId"
          class="sidebar-schedules__item"
          :class="{ 'sidebar-schedules__item--expanded': expandedId === draft.taskId, 'sidebar-schedules__item--muted': draft.state === 'created' }"
        >
          <button class="sidebar-schedules__summary" type="button" @click="toggle(draft.taskId)">
            <span class="sidebar-schedules__summary-title">{{ draft.title }}</span>
            <span class="sidebar-schedules__summary-time">{{ draft.timeLabel }}</span>
          </button>

          <div v-if="expandedId === draft.taskId" class="sidebar-schedules__card">
            <dl class="sidebar-schedules__facts">
              <dt>时间</dt>
              <dd>{{ draft.timeLabel }}</dd>
              <dt>时区</dt>
              <dd>{{ draft.timezone || '—' }}</dd>
              <template v-if="draft.location">
                <dt>地点</dt>
                <dd>{{ draft.location }}</dd>
              </template>
              <template v-if="draft.description">
                <dt>描述</dt>
                <dd>{{ draft.description }}</dd>
              </template>
              <dt>来源</dt>
              <dd>{{ draft.sourceLabel || '—' }}</dd>
            </dl>

            <p v-if="draft.sourceText" class="sidebar-schedules__source">“{{ draft.sourceText }}”</p>

            <p v-if="draft.state === 'needs_calendar'" class="sidebar-schedules__notice">
              需要先绑定日历后再创建，请先完成飞书日历授权。
            </p>
            <p v-else-if="draft.state === 'needs_input'" class="sidebar-schedules__notice">
              这条消息里的时间还不能确定，需要补充具体时间。
            </p>
            <p v-else-if="draft.state === 'creating'" class="sidebar-schedules__notice">正在创建…</p>
            <p v-else-if="draft.state === 'created'" class="sidebar-schedules__notice sidebar-schedules__notice--ok">
              已创建<span v-if="draft.eventId"> · {{ draft.eventId }}</span>
              <a v-if="draft.eventUrl" :href="draft.eventUrl" target="_blank" rel="noreferrer">打开日程</a>
            </p>
            <p v-else-if="draft.state === 'failed'" class="sidebar-schedules__notice sidebar-schedules__notice--error">
              创建失败{{ draft.errorMessage ? `：${draft.errorMessage}` : '' }}
            </p>

            <div class="sidebar-schedules__actions">
              <button
                v-if="draft.state === 'confirmable' && draft.approvalId"
                class="sidebar-schedules__confirm"
                type="button"
                :disabled="!draft.approvalId"
                @click="confirm(draft)"
              >
                确认创建
              </button>
              <button class="sidebar-schedules__dismiss" type="button" @click="dismiss(draft)">
                {{ draft.state === 'created' ? '知道了' : '收起' }}
              </button>
            </div>
          </div>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  approveAgentApproval,
  buildScheduleDraft,
  draftSortKey,
  getAgentTask,
  listAgentObservations,
  loadScheduleDrafts,
  type AgentObservation,
  type ScheduleDraft,
} from '../api/info-agent.ts'

const props = defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ (event: 'request-expand'): void }>()

const POLL_MS = 5000
const COMPLETION_TIMEOUT_MS = 90_000
const VISIBLE_LIMIT = 5

const drafts = ref<ScheduleDraft[]>([])
const finished = ref<ScheduleDraft[]>([])
const submittingTaskIDs = ref<string[]>([])
const expandedId = ref('')
const listOpen = ref(true)
const loading = ref(false)
const loadError = ref('')
const expandFirstWhenReady = ref(false)
let pollTimer: number | undefined

const pendingCount = computed(() => drafts.value.length)
const visibleDrafts = computed(() => {
  const live = [...drafts.value].sort((left, right) => draftSortKey(left) - draftSortKey(right))
  const shown = live.slice(0, VISIBLE_LIMIT)
  const shownIDs = new Set(shown.map((draft) => draft.taskId))
  const done = finished.value.filter((draft) => !shownIDs.has(draft.taskId))
  return [...shown, ...done]
})
const collapsedTitle = computed(() => (pendingCount.value ? `日程 · ${pendingCount.value} 条待确认` : '日程'))

function toggle(taskId: string) {
  expandedId.value = expandedId.value === taskId ? '' : taskId
}

// Closing a finished draft also drops it from the preview list.
function dismiss(draft: ScheduleDraft) {
  finished.value = finished.value.filter((item) => item.taskId !== draft.taskId)
  if (expandedId.value === draft.taskId) expandedId.value = ''
}

function requestExpand() {
  // The sidebar owns the collapsed flag; once it expands we open the first card.
  expandFirstWhenReady.value = true
  emit('request-expand')
}

function expandFirstIfRequested() {
  if (!expandFirstWhenReady.value || props.collapsed || !visibleDrafts.value.length) return
  expandFirstWhenReady.value = false
  expandedId.value = visibleDrafts.value[0].taskId
  listOpen.value = true
}

function sleep(ms: number) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms))
}

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    const built = await loadScheduleDrafts({ limit: 20, submittingTaskIDs: submittingTaskIDs.value })
    drafts.value = built
    // Finished drafts stay visible (with their event id) until the user closes
    // them. A list request that was already in flight when the Task completed
    // must not make the result flicker away.
    loadError.value = ''
    expandFirstIfRequested()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '日程加载失败'
  } finally {
    loading.value = false
  }
}

async function waitForCompletion(taskId: string): Promise<ScheduleDraft | null> {
  const deadline = Date.now() + COMPLETION_TIMEOUT_MS
  while (Date.now() < deadline) {
    const task = await getAgentTask(taskId).catch(() => null)
    if (task && ['succeeded', 'failed', 'unknown', 'cancelled'].includes(task.status)) {
      const observations = await listAgentObservations(taskId).catch(() => [] as AgentObservation[])
      return buildScheduleDraft({ task, observations })
    }
    await sleep(1500)
  }
  return null
}

// The completion lookup is built from the Task and its observations only, so the
// card keeps the title/time/source it already showed instead of falling back to
// placeholders.
function mergeCompletion(base: ScheduleDraft, completed: ScheduleDraft): ScheduleDraft {
  return {
    ...base,
    state: completed.state,
    taskStatus: completed.taskStatus,
    eventId: completed.eventId,
    eventUrl: completed.eventUrl,
    errorMessage: completed.errorMessage,
    missingInformation: completed.missingInformation.length ? completed.missingInformation : base.missingInformation,
  }
}

async function confirm(draft: ScheduleDraft) {
  if (!draft.approvalId || typeof draft.approvalVersion !== 'number') return
  submittingTaskIDs.value = [...submittingTaskIDs.value, draft.taskId]
  try {
    await approveAgentApproval(draft.approvalId, draft.approvalVersion)
    const completed = await waitForCompletion(draft.taskId)
    if (completed) {
      const merged = mergeCompletion(draft, completed)
      finished.value = [merged, ...finished.value.filter((item) => item.taskId !== merged.taskId)]
      expandedId.value = completed.taskId
    } else {
      loadError.value = '已提交，仍在处理中，稍后会自动刷新'
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '确认失败'
  } finally {
    submittingTaskIDs.value = submittingTaskIDs.value.filter((taskID) => taskID !== draft.taskId)
    await load()
  }
}

function onVisibility() {
  if (!document.hidden) void load()
}

watch(
  () => props.collapsed,
  (value) => {
    if (!value) expandFirstIfRequested()
  },
)

onMounted(() => {
  void load()
  pollTimer = window.setInterval(() => {
    if (!document.hidden) void load()
  }, POLL_MS)
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<style lang="less" scoped>
.sidebar-schedules {
  flex: 0 0 auto;
  margin: 0 6px 6px;
  padding: 6px;
  border-radius: 10px;
  background: var(--td-bg-color-container, #fff);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}

.sidebar-schedules--collapsed {
  margin: 0 0 6px;
  padding: 2px;
  background: transparent;
  box-shadow: none;
}

.sidebar-schedules__collapsed {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 36px;
  margin: 0 auto;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--td-text-color-secondary, #666);
  font-size: 18px;
  cursor: pointer;
}

.sidebar-schedules__collapsed:hover {
  background: var(--td-bg-color-container-hover, #eee);
  color: var(--td-brand-color, #08c46a);
}

.sidebar-schedules__badge {
  position: absolute;
  top: 0;
  right: 2px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--td-error-color, #d54941);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}

.sidebar-schedules__head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 4px 4px;
  border: none;
  background: transparent;
  color: var(--td-text-color-primary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.sidebar-schedules__head-icon {
  font-size: 16px;
  color: var(--td-text-color-secondary, #666);
}

.sidebar-schedules__head-title {
  flex: 1 1 auto;
  text-align: left;
}

.sidebar-schedules__count {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--td-brand-color, #08c46a);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}

.sidebar-schedules__head-chevron {
  font-size: 14px;
  color: var(--td-text-color-placeholder, #999);
}

.sidebar-schedules__body {
  max-height: 42vh;
  overflow-y: auto;
}

.sidebar-schedules__hint {
  margin: 6px 4px;
  color: var(--td-text-color-placeholder, #999);
  font-size: 12px;
  line-height: 1.5;
}

.sidebar-schedules__hint--error {
  color: var(--td-error-color, #d54941);
}

.sidebar-schedules__list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.sidebar-schedules__item {
  border-radius: 8px;
  overflow: hidden;
}

.sidebar-schedules__item + .sidebar-schedules__item {
  margin-top: 2px;
}

.sidebar-schedules__item--expanded {
  background: var(--td-bg-color-container-select, #f2fbf5);
}

.sidebar-schedules__item--muted {
  opacity: 0.72;
}

.sidebar-schedules__summary {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: 8px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.sidebar-schedules__summary:hover {
  background: var(--td-bg-color-container-hover, #f5f6f7);
}

.sidebar-schedules__summary-title {
  overflow: hidden;
  color: var(--td-text-color-primary);
  font-size: 12.5px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-schedules__summary-time {
  color: var(--td-brand-color, #08c46a);
  font-size: 11.5px;
}

.sidebar-schedules__card {
  padding: 4px 6px 8px;
}

.sidebar-schedules__facts {
  display: grid;
  grid-template-columns: 34px 1fr;
  gap: 2px 6px;
  margin: 0;
  font-size: 11.5px;
  line-height: 1.5;
}

.sidebar-schedules__facts dt {
  color: var(--td-text-color-placeholder, #999);
}

.sidebar-schedules__facts dd {
  margin: 0;
  color: var(--td-text-color-primary);
  word-break: break-word;
}

.sidebar-schedules__source {
  margin: 6px 0 0;
  padding: 5px 6px;
  border-radius: 6px;
  background: var(--td-bg-color-secondarycontainer, #f5f6f7);
  color: var(--td-text-color-secondary, #666);
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}

.sidebar-schedules__notice {
  margin: 6px 0 0;
  color: var(--td-text-color-secondary, #666);
  font-size: 11.5px;
  line-height: 1.5;
}

.sidebar-schedules__notice--ok {
  color: var(--td-success-color, #0aaa59);
}

.sidebar-schedules__notice--error {
  color: var(--td-error-color, #d54941);
}

.sidebar-schedules__notice a {
  margin-left: 4px;
}

.sidebar-schedules__actions {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

.sidebar-schedules__confirm {
  flex: 1 1 auto;
  height: 26px;
  border: none;
  border-radius: 6px;
  background: var(--td-brand-color, #08c46a);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
}

.sidebar-schedules__confirm:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.sidebar-schedules__dismiss {
  flex: 0 0 auto;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--td-component-stroke, #e7e7e7);
  border-radius: 6px;
  background: transparent;
  color: var(--td-text-color-secondary, #666);
  font-size: 12px;
  cursor: pointer;
}
</style>
