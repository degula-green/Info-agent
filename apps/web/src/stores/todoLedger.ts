import { defineStore } from 'pinia'
import {
  completeAgentTodo,
  listAgentTodos,
  updateAgentTodo,
  type AgentTodo,
} from '../api/info-agent.ts'

interface TodoLedgerState {
  items: AgentTodo[]
  loading: boolean
  loadError: string
  busyIDs: string[]
  itemErrors: Record<string, string>
  pollTimer: number | null
  refreshTimer: number | null
}

let activeRefresh: Promise<void> | null = null

export const useTodoLedgerStore = defineStore('todoLedger', {
  state: (): TodoLedgerState => ({
    items: [],
    loading: false,
    loadError: '',
    busyIDs: [],
    itemErrors: {},
    pollTimer: null,
    refreshTimer: null,
  }),

  getters: {
    openCount: (state) => state.items.filter((todo) => todo.status === 'open').length,
  },

  actions: {
    replaceTodo(next: AgentTodo) {
      this.items = this.items.map((todo) => (todo.todo_id === next.todo_id ? next : todo))
    },

    mergeServerItems(serverItems: AgentTodo[]) {
      const byID = new Map(serverItems.map((todo) => [todo.todo_id, todo]))
      for (const local of this.items) {
        if (this.busyIDs.includes(local.todo_id)) byID.set(local.todo_id, local)
      }
      this.items = [...byID.values()]
    },

    refresh(): Promise<void> {
      if (activeRefresh) return activeRefresh
      const work = (async () => {
        this.loading = true
        try {
          this.mergeServerItems(await listAgentTodos(['open', 'done']))
          this.loadError = ''
        } catch (error) {
          this.loadError = error instanceof Error ? error.message : '待办加载失败'
        } finally {
          this.loading = false
        }
      })()
      activeRefresh = work
      return work.finally(() => {
        if (activeRefresh === work) activeRefresh = null
      })
    },

    scheduleRefresh(delayMs = 1200) {
      if (typeof window === 'undefined') {
        void this.refresh()
        return
      }
      if (this.refreshTimer !== null) window.clearTimeout(this.refreshTimer)
      this.refreshTimer = window.setTimeout(() => {
        this.refreshTimer = null
        void this.refresh()
      }, Math.max(0, delayMs))
    },

    startPolling(intervalMs = 5000) {
      if (this.pollTimer !== null) return
      void this.refresh()
      if (typeof window === 'undefined') return
      this.pollTimer = window.setInterval(() => {
        if (!document.hidden) void this.refresh()
      }, intervalMs)
    },

    stopPolling() {
      if (this.pollTimer !== null && typeof window !== 'undefined') {
        window.clearInterval(this.pollTimer)
      }
      this.pollTimer = null
      if (this.refreshTimer !== null && typeof window !== 'undefined') {
        window.clearTimeout(this.refreshTimer)
      }
      this.refreshTimer = null
    },

    async toggleTodo(todo: AgentTodo) {
      if (this.busyIDs.includes(todo.todo_id)) return
      const target = todo.status === 'done' ? 'open' : 'done'
      const optimistic: AgentTodo = {
        ...todo,
        status: target,
        completed_at: target === 'done' ? new Date().toISOString() : null,
      }

      this.busyIDs = [...this.busyIDs, todo.todo_id]
      const remaining = { ...this.itemErrors }
      delete remaining[todo.todo_id]
      this.itemErrors = remaining
      this.replaceTodo(optimistic)

      try {
        const updated =
          target === 'done'
            ? await completeAgentTodo(todo.todo_id)
            : await updateAgentTodo(todo.todo_id, { status: 'open' })
        this.replaceTodo(updated)
      } catch (error) {
        this.replaceTodo(todo)
        this.itemErrors = {
          ...this.itemErrors,
          [todo.todo_id]: error instanceof Error ? error.message : '待办更新失败',
        }
      } finally {
        this.busyIDs = this.busyIDs.filter((id) => id !== todo.todo_id)
      }
    },
  },
})
