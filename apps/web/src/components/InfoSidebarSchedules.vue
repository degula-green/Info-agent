<template>
  <section v-if="collapsed" class="sidebar-schedules sidebar-schedules--collapsed">
    <button
      class="sidebar-schedules__collapsed"
      type="button"
      :title="collapsedTitle"
      aria-label="待办"
      @click="requestExpand"
    >
      <t-icon name="task-checked" />
      <span v-if="pendingCount" class="sidebar-schedules__badge">{{ pendingCount > 9 ? '9+' : pendingCount }}</span>
    </button>
  </section>

  <section v-else class="sidebar-schedules" aria-label="待办">
    <button class="sidebar-schedules__head" type="button" @click="listOpen = !listOpen">
      <t-icon name="task-checked" class="sidebar-schedules__head-icon" />
      <span class="sidebar-schedules__head-title">待办</span>
      <span v-if="pendingCount" class="sidebar-schedules__count">{{ pendingCount }}</span>
      <t-icon :name="listOpen ? 'chevron-down' : 'chevron-right'" class="sidebar-schedules__head-chevron" />
    </button>

    <div v-if="listOpen" class="sidebar-schedules__body">
      <p v-if="loadError" class="sidebar-schedules__hint sidebar-schedules__hint--error">{{ loadError }}</p>
      <p v-else-if="!visibleDrafts.length" class="sidebar-schedules__hint">暂无待确认待办</p>

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
            <div v-if="draft.state === 'confirmable'" class="sidebar-schedules__edit">
              <label class="sidebar-schedules__edit-label" :for="`title-${draft.taskId}`">内容</label>
              <input
                :id="`title-${draft.taskId}`"
                v-model="editTitles[draft.taskId]"
                class="sidebar-schedules__fix-input"
                type="text"
                maxlength="200"
                placeholder="待办内容"
              />

              <label class="sidebar-schedules__edit-label" :for="`due-${draft.taskId}`">截止</label>
              <t-date-picker
                :id="`due-${draft.taskId}`"
                v-model="editDueDates[draft.taskId]"
                class="sidebar-schedules__edit-date"
                value-type="YYYY-MM-DD"
                format="YYYY-MM-DD"
                placeholder="点击选择日期"
                :allow-input="false"
                clearable
                size="small"
                @clear="clearDueDate(draft)"
              />
              <p class="sidebar-schedules__fix-hint">
                点开日历选具体某一天即可，截止时间为当天 23:59。点叉可清空，清空后这条待办就没有截止时间。
              </p>
            </div>

            <dl v-else class="sidebar-schedules__facts">
              <dt>时间</dt>
              <dd>{{ draft.timeLabel }}</dd>
              <dt>时区</dt>
              <dd>{{ draft.timezone || '—' }}</dd>
              <template v-if="draft.notes">
                <dt>备注</dt>
                <dd>{{ draft.notes }}</dd>
              </template>
              <dt>发送人</dt>
              <dd>{{ draft.senderLabel || '未知' }}</dd>
              <dt>来源</dt>
              <dd>{{ draft.sourceLabel || '—' }}</dd>
            </dl>

            <p v-if="draft.sourceText" class="sidebar-schedules__source">“{{ draft.sourceText }}”</p>

            <p v-if="draft.state === 'needs_input'" class="sidebar-schedules__notice">
              这条消息里的时间不能确定，需要你补充一个明确的时间。
            </p>
            <p v-else-if="draft.state === 'creating'" class="sidebar-schedules__notice">已提交，正在创建…</p>
            <p v-else-if="draft.state === 'created'" class="sidebar-schedules__notice sidebar-schedules__notice--ok">
              已创建<span v-if="draft.eventId"> · {{ draft.eventId }}</span>
              <a v-if="draft.eventUrl" :href="draft.eventUrl" target="_blank" rel="noreferrer">打开链接</a>
            </p>
            <p v-else-if="draft.state === 'failed'" class="sidebar-schedules__notice sidebar-schedules__notice--error">
              创建失败{{ draft.errorMessage ? `：${draft.errorMessage}` : '' }}
            </p>

            <div v-if="draft.state === 'needs_input'" class="sidebar-schedules__fix">
              <input
                v-model="timeInputs[draft.taskId]"
                class="sidebar-schedules__fix-input"
                type="text"
                placeholder="例如：明天晚上八点 / 9月27日 20:00"
                @keyup.enter="submitTime(draft)"
              />
              <p class="sidebar-schedules__fix-hint">
                写清楚上午/下午/晚上，或直接用 24 小时制（如 20:00）。只写"八点"系统不会替你猜。
              </p>
              <p v-if="inputErrors[draft.taskId]" class="sidebar-schedules__hint sidebar-schedules__hint--error">
                {{ inputErrors[draft.taskId] }}
              </p>
              <button
                class="sidebar-schedules__confirm"
                type="button"
                :disabled="inputBusy.includes(draft.taskId) || !(timeInputs[draft.taskId] || '').trim()"
                @click="submitTime(draft)"
              >
                补充时间并继续
              </button>
            </div>

            <div v-if="deleteConfirmId === draft.taskId" class="sidebar-schedules__delete-confirm">
              <span class="sidebar-schedules__delete-prompt">确认删除这条待办？</span>
              <button
                class="sidebar-schedules__delete-danger"
                type="button"
                :disabled="deletingIDs.includes(draft.taskId)"
                @click="confirmDelete(draft)"
              >
                {{ deletingIDs.includes(draft.taskId) ? '删除中…' : '确认删除' }}
              </button>
              <button class="sidebar-schedules__dismiss" type="button" @click="deleteConfirmId = ''">
                取消
              </button>
            </div>
            <p
              v-if="deleteErrors[draft.taskId]"
              class="sidebar-schedules__hint sidebar-schedules__hint--error"
            >
              {{ deleteErrors[draft.taskId] }}
            </p>

            <p v-if="editErrors[draft.taskId]" class="sidebar-schedules__hint sidebar-schedules__hint--error">
              {{ editErrors[draft.taskId] }}
            </p>

            <p
              v-if="draft.verificationWarning"
              class="sidebar-schedules__notice sidebar-schedules__notice--warn"
            >
              {{ draft.verificationWarning }}
            </p>

            <div class="sidebar-schedules__actions">
              <button
                v-if="draft.state === 'confirmable' && draft.approvalId"
                class="sidebar-schedules__confirm"
                type="button"
                :disabled="!draft.approvalId || !(editTitles[draft.taskId] || '').trim()"
                @click="confirm(draft)"
              >
                确认创建
              </button>
              <button
                class="sidebar-schedules__delete"
                type="button"
                :disabled="deletingIDs.includes(draft.taskId)"
                @click="requestDelete(draft)"
              >
                <t-icon name="delete" />
                删除
              </button>
              <button
                v-if="draft.state !== 'created'"
                class="sidebar-schedules__dismiss"
                type="button"
                @click="dismiss(draft)"
              >
                收起
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
  cancelAgentTask,
  composeEditedArguments,
  composeTimeFixText,
  dayOfISO,
  draftSortKey,
  loadScheduleDrafts,
  submitAgentTaskInput,
  type ScheduleDraft,
} from '../api/info-agent.ts'
import { useTodoLedgerStore } from '../stores/todoLedger.ts'

const props = defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ (event: 'request-expand'): void }>()
const todoLedger = useTodoLedgerStore()

const POLL_MS = 5000
const VISIBLE_LIMIT = 5

const drafts = ref<ScheduleDraft[]>([])
const finished = ref<ScheduleDraft[]>([])
const submittingTaskIDs = ref<string[]>([])
// Approval the user just accepted, per Task. It keeps "creating" on the card
// until the kernel moves the Task on, without hiding a re-planned approval
// (a different approval id) that needs a fresh confirmation.
const approvedApprovalIDs = ref<Record<string, string>>({})
// Tasks whose step is running or waiting for input: they are fetched by id so
// the card stays on screen instead of vanishing when the Task leaves the
// waiting_* list.
const trackedTaskIDs = ref<string[]>([])
const timeInputs = ref<Record<string, string>>({})
// Edit buffers for a confirmable card: the title and the day the owner picks.
// They are seeded from the draft and kept in sync until the user changes them,
// so a poll that returns a fresh draft cannot silently discard an edit.
const editTitles = ref<Record<string, string>>({})
const editDueDates = ref<Record<string, string>>({})
const editErrors = ref<Record<string, string>>({})
// Once the owner clears the date, polling must not put it back.
const clearedTimeTaskIDs = ref<string[]>([])
const inputBusy = ref<string[]>([])
const inputErrors = ref<Record<string, string>>({})
const deleteConfirmId = ref('')
const deletingIDs = ref<string[]>([])
const deleteErrors = ref<Record<string, string>>({})
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
const collapsedTitle = computed(() => (pendingCount.value ? `待办 · ${pendingCount.value} 条待确认` : '待办'))

function toggle(taskId: string) {
  expandedId.value = expandedId.value === taskId ? '' : taskId
}

// Closing a finished draft also drops it from the preview list.
function dismiss(draft: ScheduleDraft) {
  finished.value = finished.value.filter((item) => item.taskId !== draft.taskId)
  if (expandedId.value === draft.taskId) expandedId.value = ''
}

// A created schedule is only a record on this page: deleting it hides the card
// and never touches the calendar event. Everything else is still pending work,
// so deleting asks first and then cancels the Task before anything is written.
function requestDelete(draft: ScheduleDraft) {
  if (draft.state === 'created') {
    dismiss(draft)
    return
  }
  deleteConfirmId.value = deleteConfirmId.value === draft.taskId ? '' : draft.taskId
}

async function confirmDelete(draft: ScheduleDraft) {
  if (deletingIDs.value.includes(draft.taskId)) return
  deletingIDs.value = [...deletingIDs.value, draft.taskId]
  const remaining = { ...deleteErrors.value }
  delete remaining[draft.taskId]
  deleteErrors.value = remaining
  try {
    // A failed Task is already terminal; only live Tasks need cancelling.
    if (draft.state !== 'failed') await cancelAgentTask(draft.taskId)
    drafts.value = drafts.value.filter((item) => item.taskId !== draft.taskId)
    finished.value = finished.value.filter((item) => item.taskId !== draft.taskId)
    trackedTaskIDs.value = trackedTaskIDs.value.filter((id) => id !== draft.taskId)
    submittingTaskIDs.value = submittingTaskIDs.value.filter((id) => id !== draft.taskId)
    forgetEditBuffers(draft.taskId)
    if (expandedId.value === draft.taskId) expandedId.value = ''
  } catch (error) {
    deleteErrors.value = {
      ...deleteErrors.value,
      [draft.taskId]: error instanceof Error ? error.message : '删除失败',
    }
  } finally {
    deletingIDs.value = deletingIDs.value.filter((id) => id !== draft.taskId)
    deleteConfirmId.value = ''
  }
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

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    const built = await loadScheduleDrafts({
      limit: 20,
      submittingTaskIDs: submittingTaskIDs.value,
      trackedTaskIDs: trackedTaskIDs.value,
    })
    // A tracked Task that reached a terminal state is shown once, with its event
    // id, and only then does it stop being tracked.
    const settled = built.filter(
      (draft) =>
        trackedTaskIDs.value.includes(draft.taskId) &&
        (draft.state === 'created' || draft.state === 'failed'),
    )
    if (settled.length) {
      const settledIDs = new Set(settled.map((draft) => draft.taskId))
      trackedTaskIDs.value = trackedTaskIDs.value.filter((taskID) => !settledIDs.has(taskID))
      for (const draft of settled) {
        if (finished.value.some((item) => item.taskId === draft.taskId)) continue
        const shown = drafts.value.find((item) => item.taskId === draft.taskId)
        finished.value = [shown ? mergeCompletion(shown, draft) : draft, ...finished.value]
      }
      if (settled.some((draft) => draft.state === 'created')) {
        void todoLedger.refresh()
      }
    }
    drafts.value = built.filter((draft) => !settled.some((item) => item.taskId === draft.taskId))
    // "Creating" is a local hint, so it must never outlive the state it
    // describes: drop it as soon as the Task is back with the user.
    submittingTaskIDs.value = submittingTaskIDs.value.filter((taskID) => {
      const draft = built.find((item) => item.taskId === taskID)
      if (!draft || draft.state !== 'creating') return false
      if (draft.taskStatus !== 'waiting_approval') return true
      // Approval versions restart with every re-plan, so identity — not the
      // version number — decides whether this is still the accepted request.
      return Boolean(draft.approvalId) && draft.approvalId === approvedApprovalIDs.value[taskID]
    })
    // Offer the parsed phrase as a starting point so the user only has to add
    // 上午/晚上 (or switch to a 24h reading) instead of retyping the message.
    for (const draft of drafts.value) {
      if (draft.state !== 'needs_input') continue
      if (timeInputs.value[draft.taskId] === undefined) {
        timeInputs.value[draft.taskId] = draft.timeExpression || ''
      }
    }
    // Seeding happens while the draft is on screen, so the poll and the initial
    // load share one path.
    for (const draft of [...drafts.value, ...finished.value]) {
      syncEditBuffers(draft)
    }
    // Finished drafts stay visible (with their event id) until the user closes
    // them. A list request that was already in flight when the Task completed
    // must not make the result flicker away.
    loadError.value = ''
    expandFirstIfRequested()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '待办加载失败'
  } finally {
    loading.value = false
  }
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

// Seeds the edit buffers for a confirmable card from the draft itself. A field
// the owner already touched keeps its value, so the 5s poll cannot race an edit.
function syncEditBuffers(draft: ScheduleDraft) {
  if (draft.state !== 'confirmable') return
  const cleared = clearedTimeTaskIDs.value.includes(draft.taskId)
  // "" is a real buffered value (the owner cleared it), so "absent" and "empty"
  // must be told apart: only an absent key gets seeded.
  if (editTitles.value[draft.taskId] === undefined) {
    editTitles.value = { ...editTitles.value, [draft.taskId]: draft.title }
  }
  if (editDueDates.value[draft.taskId] === undefined) {
    const seeded = cleared ? '' : dayOfISO(draft.dueAt, draft.timezone || 'Asia/Shanghai')
    editDueDates.value = { ...editDueDates.value, [draft.taskId]: seeded }
  }
}

// Drops the per-card buffers so a reused task id cannot inherit an old edit.
function forgetEditBuffers(taskId: string) {
  if (taskId in editTitles.value) {
    const next = { ...editTitles.value }
    delete next[taskId]
    editTitles.value = next
  }
  if (taskId in editDueDates.value) {
    const next = { ...editDueDates.value }
    delete next[taskId]
    editDueDates.value = next
  }
  if (taskId in editErrors.value) {
    const next = { ...editErrors.value }
    delete next[taskId]
    editErrors.value = next
  }
  clearedTimeTaskIDs.value = clearedTimeTaskIDs.value.filter((id) => id !== taskId)
}

// TDesign clears the model and emits @clear; the choice is remembered so the
// next poll does not restore the date the owner just removed.
function clearDueDate(draft: ScheduleDraft) {
  editDueDates.value = { ...editDueDates.value, [draft.taskId]: '' }
  if (!clearedTimeTaskIDs.value.includes(draft.taskId)) {
    clearedTimeTaskIDs.value = [...clearedTimeTaskIDs.value, draft.taskId]
  }
}

async function confirm(draft: ScheduleDraft) {
  if (!draft.approvalId || typeof draft.approvalVersion !== 'number') return
  const title = (editTitles.value[draft.taskId] ?? draft.title).trim()
  if (!title) return
  submittingTaskIDs.value = [...submittingTaskIDs.value, draft.taskId]
  const remaining = { ...editErrors.value }
  delete remaining[draft.taskId]
  editErrors.value = remaining
  try {
    // The card is the only place these values exist, so they are sent with the
    // approval: the backend overwrites the Step arguments and re-fingerprints
    // them before executing.
    const { arguments: edited } = composeEditedArguments(draft, {
      title,
      dueDate: editDueDates.value[draft.taskId] || '',
    })
    edited.title = title
    const original = { ...(draft.arguments || {}) }
    const merged = { ...original, ...edited }
    // Keep the keys the capability does not accept out of the payload.
    delete (merged as Record<string, unknown>).start_time
    delete (merged as Record<string, unknown>).end_time
    delete (merged as Record<string, unknown>).time_expression
    delete (merged as Record<string, unknown>).location
    delete (merged as Record<string, unknown>).description
    await approveAgentApproval(draft.approvalId, draft.approvalVersion, merged)
    todoLedger.scheduleRefresh()
    // Keep the card in place: it leaves the waiting_* list the moment the step
    // starts, so without tracking it would vanish until the result arrives.
    approvedApprovalIDs.value = { ...approvedApprovalIDs.value, [draft.taskId]: draft.approvalId }
    trackedTaskIDs.value = [...new Set([...trackedTaskIDs.value, draft.taskId])]
    expandedId.value = draft.taskId
  } catch (error) {
    // An expired or superseded approval is about this one card, so it must not
    // hide the whole list behind a load error.
    editErrors.value = {
      ...editErrors.value,
      [draft.taskId]: error instanceof Error ? error.message : '确认失败',
    }
    submittingTaskIDs.value = submittingTaskIDs.value.filter((taskID) => taskID !== draft.taskId)
    trackedTaskIDs.value = trackedTaskIDs.value.filter((taskID) => taskID !== draft.taskId)
  }
  await load()
}

// Supplies the missing time and lets the kernel re-plan from the new sentence.
async function submitTime(draft: ScheduleDraft) {
  const replacement = (timeInputs.value[draft.taskId] || '').trim()
  if (!replacement || inputBusy.value.includes(draft.taskId)) return
  inputBusy.value = [...inputBusy.value, draft.taskId]
  const remaining = { ...inputErrors.value }
  delete remaining[draft.taskId]
  inputErrors.value = remaining
  try {
    const nextText = composeTimeFixText(draft.sourceText, draft.timeExpression || '', replacement)
    await submitAgentTaskInput(draft.taskId, { text: nextText })
    trackedTaskIDs.value = [...new Set([...trackedTaskIDs.value, draft.taskId])]
    submittingTaskIDs.value = [...new Set([...submittingTaskIDs.value, draft.taskId])]
  } catch (error) {
    inputErrors.value = {
      ...inputErrors.value,
      [draft.taskId]: error instanceof Error ? error.message : '补充时间失败',
    }
  } finally {
    inputBusy.value = inputBusy.value.filter((taskID) => taskID !== draft.taskId)
  }
  await load()
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

.sidebar-schedules__notice--warn {
  color: var(--td-warning-color, #e37318);
}

.sidebar-schedules__notice a {
  margin-left: 4px;
}

.sidebar-schedules__edit {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 6px;
}

.sidebar-schedules__edit-label {
  color: var(--td-text-color-placeholder, #999);
  font-size: 11px;
}

.sidebar-schedules__edit-date {
  width: 100%;
}

.sidebar-schedules__fix {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 6px;
}

.sidebar-schedules__fix-input {
  height: 26px;
  padding: 0 8px;
  border: 1px solid var(--td-component-stroke, #e7e7e7);
  border-radius: 6px;
  background: var(--td-bg-color-container, #fff);
  color: var(--td-text-color-primary);
  font-size: 12px;
}

.sidebar-schedules__fix-input:focus {
  border-color: var(--td-brand-color, #08c46a);
  outline: none;
}

.sidebar-schedules__fix-hint {
  margin: 0;
  color: var(--td-text-color-placeholder, #999);
  font-size: 11px;
  line-height: 1.5;
}

.sidebar-schedules__actions {
  display: flex;
  flex-wrap: wrap;
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

.sidebar-schedules__delete {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--td-error-color-3, #f5c2c2);
  border-radius: 6px;
  background: transparent;
  color: var(--td-error-color-6, #d54941);
  font-size: 12px;
  cursor: pointer;
}

.sidebar-schedules__delete:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.sidebar-schedules__delete-confirm {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--td-error-color-1, #fff1f0);
}

.sidebar-schedules__delete-prompt {
  flex: 1 1 100%;
  color: var(--td-text-color-secondary, #666);
  font-size: 11px;
  line-height: 1.4;
}

.sidebar-schedules__delete-danger {
  flex: 0 0 auto;
  height: 24px;
  padding: 0 10px;
  border: none;
  border-radius: 6px;
  background: var(--td-error-color-6, #d54941);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
}

.sidebar-schedules__delete-danger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
