<template>
  <Teleport to="body">
    <article
      ref="panelRef"
      class="todo-float"
      :class="{ 'todo-float--collapsed': collapsed, 'todo-float--dragging': dragging }"
      :style="panelStyle"
      aria-label="待办"
    >
      <button
        v-if="collapsed"
        class="todo-float__collapsed"
        type="button"
        :aria-label="`展开待办，${openCount} 条未完成`"
        title="展开待办"
        @click="collapsed = false"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M7.5 4.75h9a2.75 2.75 0 0 1 2.75 2.75v9a2.75 2.75 0 0 1-2.75 2.75h-9A2.75 2.75 0 0 1 4.75 16.5v-9A2.75 2.75 0 0 1 7.5 4.75Z" />
          <path d="m8.5 12.1 2.2 2.2 4.8-5" />
        </svg>
        <span v-if="openCount" class="todo-float__badge">{{ openCount > 9 ? '9+' : openCount }}</span>
      </button>

      <section v-else class="todo-float__shell">
        <header ref="headerRef" class="todo-float__header" @pointerdown="startDrag">
          <button
            class="todo-float__switch"
            type="button"
            :aria-label="activeTab === 'open' ? '待办，当前显示未完成，点击查看已完成' : '待办，当前显示已完成，点击查看未完成'"
            @click="switchTab"
          >
            <span class="todo-float__title">待办</span>
            <span class="todo-float__scope">{{ activeTab === 'open' ? '未完成' : '已完成' }}</span>
            <span class="todo-float__count">{{ visibleItems.length }}</span>
          </button>
          <button
            class="todo-float__collapse"
            type="button"
            data-no-drag
            aria-label="收起待办"
            title="收起待办"
            @click="collapsed = true"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M6 12h12" />
              <path d="m9 9-3 3 3 3" />
            </svg>
          </button>
        </header>

        <div class="todo-float__body">
          <p v-if="loadError" class="todo-float__hint todo-float__hint--error">{{ loadError }}</p>
          <div v-else-if="loading && !todos.length" class="todo-float__empty">
            <p>正在加载待办</p>
          </div>
          <div v-else-if="!visibleItems.length" class="todo-float__empty">
            <span class="todo-float__empty-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M7.5 4.75h9a2.75 2.75 0 0 1 2.75 2.75v9a2.75 2.75 0 0 1-2.75 2.75h-9A2.75 2.75 0 0 1 4.75 16.5v-9A2.75 2.75 0 0 1 7.5 4.75Z" />
                <path d="m8.5 12.1 2.2 2.2 4.8-5" />
              </svg>
            </span>
            <p>{{ activeTab === 'open' ? '暂无未完成待办' : '暂无已完成待办' }}</p>
          </div>
          <ul v-else class="todo-float__list">
            <li
              v-for="todo in visibleItems"
              :key="todo.todo_id"
              class="todo-float__card"
              :class="{ 'todo-float__card--done': todo.status === 'done' }"
            >
              <button
                class="todo-float__check"
                type="button"
                :disabled="busyIDs.includes(todo.todo_id)"
                :aria-label="todo.status === 'done' ? `恢复待办：${todo.title}` : `完成待办：${todo.title}`"
                :aria-pressed="todo.status === 'done'"
                @click="toggleTodo(todo)"
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <path d="m3.2 8.3 3 3 6.6-7" />
                </svg>
              </button>

              <div class="todo-float__card-main">
                <div class="todo-float__source">
                  <span class="todo-float__source-dot" aria-hidden="true"></span>
                  <span>{{ agentTodoSourceLabel(todo) }}</span>
                </div>
                <p class="todo-float__card-title">{{ todo.title }}</p>
                <div class="todo-float__footer">
                  <span class="todo-float__time">{{ agentTodoTimeLabel(todo) }}</span>
                  <span class="todo-float__avatar" aria-hidden="true">{{ agentTodoSourceInitial(todo) }}</span>
                </div>
                <p v-if="itemErrors[todo.todo_id]" class="todo-float__hint todo-float__hint--error">
                  {{ itemErrors[todo.todo_id] }}
                </p>
              </div>
            </li>
          </ul>
        </div>
      </section>
    </article>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  agentTodoSourceInitial,
  agentTodoSourceLabel,
  agentTodoTimeLabel,
  completeAgentTodo,
  listAgentTodos,
  sortAgentTodos,
  updateAgentTodo,
  type AgentTodo,
} from '../api/info-agent.ts'

type TodoTab = 'open' | 'done'
type DragState = {
  pointerId: number
  startX: number
  startY: number
  originX: number
  originY: number
  moved: boolean
}

const POLL_MS = 5000
const DEFAULT_GAP = 24
const EDGE_GAP = 12
const PANEL_WIDTH = 344
const COLLAPSED_WIDTH = 56

const panelRef = ref<HTMLElement | null>(null)
const headerRef = ref<HTMLElement | null>(null)
const todos = ref<AgentTodo[]>([])
const activeTab = ref<TodoTab>('open')
const collapsed = ref(false)
const loading = ref(false)
const dragging = ref(false)
const loadError = ref('')
const busyIDs = ref<string[]>([])
const itemErrors = ref<Record<string, string>>({})
const position = ref({ x: 0, y: DEFAULT_GAP })
let dragState: DragState | null = null
let suppressTabClick = false
let pollTimer: number | undefined

const openCount = computed(() => todos.value.filter((todo) => todo.status === 'open').length)
const visibleItems = computed(() => sortAgentTodos(todos.value, activeTab.value))
const panelStyle = computed(() => ({
  left: `${position.value.x}px`,
  top: `${position.value.y}px`,
}))

function mergePolledTodos(serverTodos: AgentTodo[]) {
  const byID = new Map(serverTodos.map((todo) => [todo.todo_id, todo]))
  for (const local of todos.value) {
    if (busyIDs.value.includes(local.todo_id)) byID.set(local.todo_id, local)
  }
  todos.value = [...byID.values()]
}

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    mergePolledTodos(await listAgentTodos(['open', 'done']))
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '待办加载失败'
  } finally {
    loading.value = false
  }
}

function replaceTodo(next: AgentTodo) {
  todos.value = todos.value.map((todo) => (todo.todo_id === next.todo_id ? next : todo))
}

async function toggleTodo(todo: AgentTodo) {
  if (busyIDs.value.includes(todo.todo_id)) return
  const target: TodoTab = todo.status === 'done' ? 'open' : 'done'
  const optimistic: AgentTodo = {
    ...todo,
    status: target,
    completed_at: target === 'done' ? new Date().toISOString() : null,
  }

  busyIDs.value = [...busyIDs.value, todo.todo_id]
  const remaining = { ...itemErrors.value }
  delete remaining[todo.todo_id]
  itemErrors.value = remaining
  replaceTodo(optimistic)

  try {
    const updated =
      target === 'done'
        ? await completeAgentTodo(todo.todo_id)
        : await updateAgentTodo(todo.todo_id, { status: 'open' })
    replaceTodo(updated)
  } catch (error) {
    replaceTodo(todo)
    itemErrors.value = {
      ...itemErrors.value,
      [todo.todo_id]: error instanceof Error ? error.message : '待办更新失败',
    }
  } finally {
    busyIDs.value = busyIDs.value.filter((id) => id !== todo.todo_id)
  }
}

function switchTab() {
  if (suppressTabClick) {
    suppressTabClick = false
    return
  }
  activeTab.value = activeTab.value === 'open' ? 'done' : 'open'
}

function clampPosition() {
  const panel = panelRef.value
  if (!panel) return
  const width = panel.offsetWidth || (collapsed.value ? COLLAPSED_WIDTH : PANEL_WIDTH)
  const height = panel.offsetHeight || COLLAPSED_WIDTH
  const maxX = Math.max(EDGE_GAP, window.innerWidth - width - EDGE_GAP)
  const maxY = Math.max(EDGE_GAP, window.innerHeight - height - EDGE_GAP)
  position.value = {
    x: Math.min(Math.max(EDGE_GAP, position.value.x), maxX),
    y: Math.min(Math.max(EDGE_GAP, position.value.y), maxY),
  }
}

function startDrag(event: PointerEvent) {
  if (event.button !== 0 || (event.target as Element | null)?.closest('[data-no-drag]')) return
  dragState = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originX: position.value.x,
    originY: position.value.y,
    moved: false,
  }
}

function moveDrag(event: PointerEvent) {
  if (!dragState || dragState.pointerId !== event.pointerId) return
  const nextX = dragState.originX + event.clientX - dragState.startX
  const nextY = dragState.originY + event.clientY - dragState.startY
  if (!dragState.moved && Math.hypot(nextX - dragState.originX, nextY - dragState.originY) > 4) {
    dragState.moved = true
    dragging.value = true
    headerRef.value?.setPointerCapture(event.pointerId)
    event.preventDefault()
  }
  if (!dragState.moved) return
  position.value = { x: nextX, y: nextY }
}

function endDrag(event: PointerEvent) {
  if (!dragState || dragState.pointerId !== event.pointerId) return
  if (dragState.moved && headerRef.value?.hasPointerCapture(event.pointerId)) {
    headerRef.value.releasePointerCapture(event.pointerId)
  }
  if (dragState.moved) {
    suppressTabClick = true
    window.setTimeout(() => {
      suppressTabClick = false
    }, 0)
  }
  dragState = null
  dragging.value = false
  clampPosition()
}

function onResize() {
  requestAnimationFrame(() => {
    clampPosition()
  })
}

function onVisibilityChange() {
  if (!document.hidden) void load()
}

onMounted(() => {
  requestAnimationFrame(() => {
    position.value = {
      x: Math.max(EDGE_GAP, window.innerWidth - PANEL_WIDTH - DEFAULT_GAP),
      y: DEFAULT_GAP,
    }
    clampPosition()
  })
  void load()
  pollTimer = window.setInterval(() => {
    if (!document.hidden) void load()
  }, POLL_MS)
  window.addEventListener('pointermove', moveDrag)
  window.addEventListener('pointerup', endDrag)
  window.addEventListener('pointercancel', endDrag)
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  window.removeEventListener('pointermove', moveDrag)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style lang="less" scoped>
.todo-float {
  position: fixed;
  z-index: 90;
  width: min(344px, calc(100vw - 24px));
  overflow: hidden;
  border: 1px solid #2a2a2a;
  border-radius: 18px;
  background: #141414;
  box-shadow:
    0 22px 54px rgba(0, 0, 0, 0.3),
    0 2px 8px rgba(0, 0, 0, 0.26);
  color: #f4f4f4;
  font-family: inherit;
  transition:
    width 220ms cubic-bezier(0.22, 1, 0.36, 1),
    height 220ms cubic-bezier(0.22, 1, 0.36, 1),
    border-radius 220ms ease,
    box-shadow 160ms ease;
  will-change: left, top;
}

.todo-float--collapsed {
  width: 56px;
  height: 56px;
}

.todo-float--dragging {
  user-select: none;
  box-shadow:
    0 28px 68px rgba(0, 0, 0, 0.4),
    0 3px 10px rgba(0, 0, 0, 0.3);
}

.todo-float__shell {
  width: 100%;
}

.todo-float__header {
  display: flex;
  align-items: center;
  min-height: 56px;
  padding: 8px 8px 8px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  cursor: grab;
  touch-action: none;
}

.todo-float--dragging .todo-float__header {
  cursor: grabbing;
}

.todo-float__switch {
  display: flex;
  align-items: center;
  min-width: 0;
  min-height: 40px;
  flex: 1 1 auto;
  gap: 7px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: inherit;
}

.todo-float__title {
  flex: 0 0 auto;
  color: #f4f4f4;
  font-size: 19px;
  font-weight: 700;
}

.todo-float__scope {
  overflow: hidden;
  color: #777;
  font-size: 12px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.todo-float__count {
  display: inline-grid;
  min-width: 24px;
  height: 22px;
  place-items: center;
  padding: 0 7px;
  border-radius: 11px;
  background: #242424;
  color: #aaa;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.todo-float__collapse {
  display: inline-grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: #8d8d8d;
  cursor: pointer;
}

.todo-float__collapse:hover {
  background: #242424;
  color: #e8e8e8;
}

.todo-float__collapse svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
}

.todo-float__body {
  max-height: min(570px, calc(100vh - 112px));
  padding: 11px;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-color: #454545 transparent;
  scrollbar-width: thin;
}

.todo-float__list {
  display: grid;
  gap: 11px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.todo-float__card {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr);
  gap: 11px;
  min-height: 126px;
  padding: 15px 15px 14px 13px;
  border: 1px solid #2c2c2c;
  border-radius: 13px;
  background: #1c1c1c;
  transition:
    border-color 150ms ease,
    background 150ms ease;
}

.todo-float__card:hover {
  border-color: #454545;
  background: #222;
}

.todo-float__check {
  display: inline-grid;
  width: 22px;
  height: 22px;
  place-items: center;
  align-self: start;
  margin-top: 1px;
  padding: 0;
  border: 1.5px solid #5c5c5c;
  border-radius: 50%;
  background: transparent;
  color: #fff;
  cursor: pointer;
}

.todo-float__check:hover {
  border-color: #adadad;
  background: rgba(255, 255, 255, 0.07);
}

.todo-float__check:disabled {
  cursor: wait;
  opacity: 0.62;
}

.todo-float__check svg {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2.2;
  stroke-linecap: round;
  stroke-linejoin: round;
  opacity: 0;
}

.todo-float__card--done .todo-float__check {
  border-color: #777;
  background: #303030;
}

.todo-float__card--done .todo-float__check svg {
  opacity: 1;
}

.todo-float__card-main {
  min-width: 0;
}

.todo-float__source {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 7px;
  margin-bottom: 9px;
  overflow: hidden;
  color: #888;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.todo-float__source-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
  background: #6b6b6b;
}

.todo-float__card-title {
  display: -webkit-box;
  min-height: 44px;
  margin: 0;
  overflow: hidden;
  color: #f0f0f0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.42;
  text-decoration: line-through;
  text-decoration-color: transparent;
  text-overflow: ellipsis;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.todo-float__card--done .todo-float__card-title {
  color: #8d8d8d;
  text-decoration-color: #8d8d8d;
}

.todo-float__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 15px;
}

.todo-float__time {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  max-width: calc(100% - 44px);
  padding: 4px 11px;
  overflow: hidden;
  border-radius: 14px;
  background: #262626;
  color: #a8a8a8;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.todo-float__avatar {
  display: inline-grid;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  place-items: center;
  border-radius: 50%;
  background: #2b2b2b;
  color: #a3a3a3;
  font-size: 12px;
  font-weight: 700;
}

.todo-float__empty {
  display: grid;
  min-height: 210px;
  place-items: center;
  padding: 30px 20px;
  text-align: center;
}

.todo-float__empty p,
.todo-float__hint {
  margin: 0;
  color: #777;
  font-size: 12px;
  line-height: 1.5;
}

.todo-float__empty-icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  margin-bottom: 10px;
  border: 1px solid #303030;
  border-radius: 12px;
  color: #666;
}

.todo-float__empty-icon svg {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.todo-float__hint {
  padding: 16px 10px;
}

.todo-float__hint--error {
  color: #c98c8c;
}

.todo-float__card .todo-float__hint {
  padding: 4px 0 0;
  font-size: 10.5px;
}

.todo-float__collapsed {
  position: relative;
  display: grid;
  width: 56px;
  height: 56px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: inherit;
  background: transparent;
  color: #d0d0d0;
  cursor: pointer;
}

.todo-float__collapsed:hover {
  background: #1c1c1c;
}

.todo-float__collapsed svg {
  width: 22px;
  height: 22px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.todo-float__badge {
  position: absolute;
  top: 3px;
  right: 3px;
  display: inline-grid;
  min-width: 17px;
  height: 17px;
  place-items: center;
  padding: 0 4px;
  border: 2px solid #141414;
  border-radius: 9px;
  background: #e7e7e7;
  color: #111;
  font-size: 9px;
  font-weight: 700;
}

@media (max-width: 520px) {
  .todo-float {
    width: calc(100vw - 24px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .todo-float {
    transition-duration: 0.01ms;
  }
}
</style>
