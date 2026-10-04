export type TodoPanelTab = 'open' | 'done'
export type TodoPanelExpandX = 'left' | 'right'
export type TodoPanelExpandY = 'up' | 'down'

export interface TodoPanelAnchor {
  x: number
  y: number
}

export interface TodoPanelViewport {
  width: number
  height: number
}

export interface TodoPanelSize {
  width: number
  height: number
}

export interface TodoPanelState extends TodoPanelAnchor {
  version: 1
  collapsed: boolean
  activeTab: TodoPanelTab
  opacity: number
}

export interface TodoPanelPlacement extends TodoPanelAnchor {
  expandX: TodoPanelExpandX
  expandY: TodoPanelExpandY
}

export type TodoPanelStorage = Pick<Storage, 'getItem' | 'setItem'>

export const TODO_PANEL_STORAGE_KEY = 'info-agent.todo-panel.v1'
export const TODO_PANEL_COLLAPSED_SIZE = 56
export const TODO_PANEL_DEFAULT_GAP = 24
export const TODO_PANEL_EDGE_GAP = 12
export const TODO_PANEL_DEFAULT_OPACITY = 1
export const TODO_PANEL_MIN_OPACITY = 0.5
export const TODO_PANEL_MAX_OPACITY = 1

function browserStorage(): TodoPanelStorage | null {
  if (typeof window === 'undefined') return null
  return window.localStorage
}

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

export function defaultTodoPanelAnchor(viewport: TodoPanelViewport): TodoPanelAnchor {
  return {
    x: Math.max(
      TODO_PANEL_EDGE_GAP,
      viewport.width - TODO_PANEL_COLLAPSED_SIZE - TODO_PANEL_DEFAULT_GAP,
    ),
    y: TODO_PANEL_DEFAULT_GAP,
  }
}

export function defaultTodoPanelState(viewport: TodoPanelViewport): TodoPanelState {
  return {
    version: 1,
    ...defaultTodoPanelAnchor(viewport),
    collapsed: false,
    activeTab: 'open',
    opacity: TODO_PANEL_DEFAULT_OPACITY,
  }
}

export function clampTodoPanelAnchor(
  anchor: TodoPanelAnchor,
  viewport: TodoPanelViewport,
  size: TodoPanelSize = {
    width: TODO_PANEL_COLLAPSED_SIZE,
    height: TODO_PANEL_COLLAPSED_SIZE,
  },
  edgeGap = TODO_PANEL_EDGE_GAP,
): TodoPanelAnchor {
  const width = Math.max(0, size.width)
  const height = Math.max(0, size.height)
  return {
    x: clamp(
      finiteNumber(anchor.x, edgeGap),
      edgeGap,
      Math.max(edgeGap, viewport.width - width - edgeGap),
    ),
    y: clamp(
      finiteNumber(anchor.y, edgeGap),
      edgeGap,
      Math.max(edgeGap, viewport.height - height - edgeGap),
    ),
  }
}

export function todoPanelPlacement(
  anchor: TodoPanelAnchor,
  viewport: TodoPanelViewport,
  panelSize: TodoPanelSize,
  edgeGap = TODO_PANEL_EDGE_GAP,
): TodoPanelPlacement {
  const clamped = clampTodoPanelAnchor(anchor, viewport, undefined, edgeGap)
  const width = Math.max(0, panelSize.width)
  const height = Math.max(0, panelSize.height)
  const iconRight = clamped.x + TODO_PANEL_COLLAPSED_SIZE
  const iconBottom = clamped.y + TODO_PANEL_COLLAPSED_SIZE
  const spaceRight = viewport.width - edgeGap - iconRight
  const spaceLeft = clamped.x - edgeGap
  const spaceBelow = viewport.height - edgeGap - iconBottom
  const spaceAbove = clamped.y - edgeGap
  const expandX: TodoPanelExpandX = width <= spaceRight || spaceRight >= spaceLeft ? 'right' : 'left'
  const expandY: TodoPanelExpandY =
    height <= spaceBelow || spaceBelow >= spaceAbove ? 'down' : 'up'
  const left = expandX === 'right' ? clamped.x : iconRight - width
  const top = expandY === 'down' ? clamped.y : iconBottom - height

  return {
    x: clamp(left, edgeGap, Math.max(edgeGap, viewport.width - width - edgeGap)),
    y: clamp(top, edgeGap, Math.max(edgeGap, viewport.height - height - edgeGap)),
    expandX,
    expandY,
  }
}

export function parseTodoPanelState(raw: string | null): TodoPanelState | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    if (value.version !== 1) return null
    const x = Number(value.x)
    const y = Number(value.y)
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null
    return {
      version: 1,
      x,
      y,
      collapsed: Boolean(value.collapsed),
      activeTab: value.activeTab === 'done' ? 'done' : 'open',
      opacity: clamp(
        finiteNumber(value.opacity, TODO_PANEL_DEFAULT_OPACITY),
        TODO_PANEL_MIN_OPACITY,
        TODO_PANEL_MAX_OPACITY,
      ),
    }
  } catch {
    return null
  }
}

export function loadTodoPanelState(
  storage: TodoPanelStorage | null = browserStorage(),
): TodoPanelState | null {
  if (!storage) return null
  try {
    return parseTodoPanelState(storage.getItem(TODO_PANEL_STORAGE_KEY))
  } catch {
    return null
  }
}

export function saveTodoPanelState(
  state: TodoPanelState,
  storage: TodoPanelStorage | null = browserStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(TODO_PANEL_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // A blocked or full localStorage must not break the panel.
  }
}
