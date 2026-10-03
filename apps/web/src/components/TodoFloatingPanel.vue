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
        @pointerdown="startDrag"
        @click="expandCollapsed"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M7.5 4.75h9a2.75 2.75 0 0 1 2.75 2.75v9a2.75 2.75 0 0 1-2.75 2.75h-9A2.75 2.75 0 0 1 4.75 16.5v-9A2.75 2.75 0 0 1 7.5 4.75Z" />
          <path d="m8.5 12.1 2.2 2.2 4.8-5" />
        </svg>
        <span v-if="openCount" class="todo-float__badge">{{ openCount > 9 ? '9+' : openCount }}</span>
      </button>

      <section v-else class="todo-float__shell">
        <header class="todo-float__header" @pointerdown="startDrag">
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
            @click="collapsePanel"
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
import { storeToRefs } from 'pinia'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  agentTodoSourceInitial,
  agentTodoSourceLabel,
  agentTodoTimeLabel,
  sortAgentTodos,
  type AgentTodo,
} from '../api/info-agent.ts'
import { useTodoLedgerStore } from '../stores/todoLedger.ts'
import {
  clampTodoPanelAnchor,
  defaultTodoPanelState,
  loadTodoPanelState,
  saveTodoPanelState,
  todoPanelPlacement,
  type TodoPanelAnchor,
  type TodoPanelTab,
} from '../utils/todo-panel-state.ts'

type DragState = {
  pointerId: number
  startX: number
  startY: number
  originX: number
  originY: number
  moved: boolean
}

const PANEL_WIDTH = 344

const panelRef = ref<HTMLElement | null>(null)
const ledger = useTodoLedgerStore()
const { items: todos, loading, loadError, busyIDs, itemErrors, openCount } = storeToRefs(ledger)
const initialViewport = {
  width: typeof window === 'undefined' ? 1280 : window.innerWidth,
  height: typeof window === 'undefined' ? 720 : window.innerHeight,
}
const persistedPanelState = loadTodoPanelState()
const initialState = persistedPanelState || defaultTodoPanelState(initialViewport)
const activeTab = ref<TodoPanelTab>(initialState.activeTab)
const collapsed = ref(initialState.collapsed)
const dragging = ref(false)
const position = ref<TodoPanelAnchor>({ x: initialState.x, y: initialState.y })
const viewport = ref(initialViewport)
const panelSize = ref({ width: PANEL_WIDTH, height: 420 })
let dragState: DragState | null = null
let suppressClickAfterDrag = false
let panelResizeObserver: ResizeObserver | undefined

const visibleItems = computed(() => sortAgentTodos(todos.value, activeTab.value))
const placement = computed(() =>
  todoPanelPlacement(position.value, viewport.value, panelSize.value),
)
const panelStyle = computed(() =>
  collapsed.value
    ? {
        left: `${position.value.x}px`,
        top: `${position.value.y}px`,
      }
    : {
        left: `${placement.value.x}px`,
        top: `${placement.value.y}px`,
      },
)

function toggleTodo(todo: AgentTodo) {
  void ledger.toggleTodo(todo)
}

function switchTab() {
  if (consumeSuppressedClick()) return
  activeTab.value = activeTab.value === 'open' ? 'done' : 'open'
  persistPanelState()
}

function expandCollapsed() {
  if (consumeSuppressedClick()) return
  collapsed.value = false
  persistPanelState()
  requestAnimationFrame(syncPanelSize)
}

function collapsePanel() {
  collapsed.value = true
  persistPanelState()
}

function consumeSuppressedClick(): boolean {
  if (!suppressClickAfterDrag) return false
  suppressClickAfterDrag = false
  return true
}

function persistPanelState() {
  saveTodoPanelState({
    version: 1,
    x: position.value.x,
    y: position.value.y,
    collapsed: collapsed.value,
    activeTab: activeTab.value,
  })
}

function clampPosition() {
  position.value = clampTodoPanelAnchor(position.value, viewport.value)
}

function syncPanelSize() {
  const panel = panelRef.value
  if (!panel || collapsed.value) return
  panelSize.value = {
    width: panel.offsetWidth || PANEL_WIDTH,
    height: panel.offsetHeight || panelSize.value.height,
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
    panelRef.value?.setPointerCapture(event.pointerId)
    event.preventDefault()
  }
  if (!dragState.moved) return
  position.value = { x: nextX, y: nextY }
}

function endDrag(event: PointerEvent) {
  if (!dragState || dragState.pointerId !== event.pointerId) return
  if (dragState.moved && panelRef.value?.hasPointerCapture(event.pointerId)) {
    panelRef.value.releasePointerCapture(event.pointerId)
  }
  if (dragState.moved) {
    suppressClickAfterDrag = true
    window.setTimeout(() => {
      suppressClickAfterDrag = false
    }, 0)
  }
  dragState = null
  dragging.value = false
  clampPosition()
  persistPanelState()
}

function onResize() {
  viewport.value = {
    width: window.innerWidth,
    height: window.innerHeight,
  }
  requestAnimationFrame(() => {
    clampPosition()
    persistPanelState()
  })
}

function onVisibilityChange() {
  if (!document.hidden) void ledger.refresh()
}

onMounted(() => {
  requestAnimationFrame(() => {
    viewport.value = {
      width: window.innerWidth,
      height: window.innerHeight,
    }
    clampPosition()
    syncPanelSize()
    persistPanelState()
    const panel = panelRef.value
    if (panel && typeof ResizeObserver !== 'undefined') {
      panelResizeObserver = new ResizeObserver(() => {
        requestAnimationFrame(syncPanelSize)
      })
      panelResizeObserver.observe(panel)
    }
  })
  ledger.startPolling()
  window.addEventListener('pointermove', moveDrag)
  window.addEventListener('pointerup', endDrag)
  window.addEventListener('pointercancel', endDrag)
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  ledger.stopPolling()
  panelResizeObserver?.disconnect()
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
  cursor: grab;
  touch-action: none;
}

.todo-float--dragging {
  user-select: none;
  box-shadow:
    0 28px 68px rgba(0, 0, 0, 0.4),
    0 3px 10px rgba(0, 0, 0, 0.3);
}

.todo-float--collapsed.todo-float--dragging,
.todo-float--collapsed.todo-float--dragging .todo-float__collapsed {
  cursor: grabbing;
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
  cursor: grab;
  touch-action: none;
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
