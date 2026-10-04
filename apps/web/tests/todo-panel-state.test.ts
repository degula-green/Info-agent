import assert from 'node:assert/strict'
import test from 'node:test'
import {
  TODO_PANEL_COLLAPSED_SIZE,
  TODO_PANEL_DEFAULT_OPACITY,
  TODO_PANEL_STORAGE_KEY,
  clampTodoPanelAnchor,
  loadTodoPanelState,
  parseTodoPanelState,
  saveTodoPanelState,
  todoPanelPlacement,
} from '../src/utils/todo-panel-state.ts'

test('a moved panel is clamped back inside the visible viewport', () => {
  const viewport = { width: 1280, height: 720 }

  assert.deepEqual(clampTodoPanelAnchor({ x: -40, y: 900 }, viewport), {
    x: 12,
    y: 720 - TODO_PANEL_COLLAPSED_SIZE - 12,
  })
  assert.deepEqual(clampTodoPanelAnchor({ x: 2000, y: -20 }, viewport), {
    x: 1280 - TODO_PANEL_COLLAPSED_SIZE - 12,
    y: 12,
  })
})

test('the expanded card opens toward the side with enough space', () => {
  const viewport = { width: 1280, height: 720 }
  const panel = { width: 344, height: 450 }

  assert.deepEqual(todoPanelPlacement({ x: 1100, y: 40 }, viewport, panel), {
    x: 1100 + TODO_PANEL_COLLAPSED_SIZE - 344,
    y: 40,
    expandX: 'left',
    expandY: 'down',
  })
  assert.deepEqual(todoPanelPlacement({ x: 20, y: 600 }, viewport, panel), {
    x: 20,
    y: 600 + TODO_PANEL_COLLAPSED_SIZE - 450,
    expandX: 'right',
    expandY: 'up',
  })
})

test('panel state survives a storage round trip and rejects bad data', () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  }

  saveTodoPanelState(
    { version: 1, x: 640, y: 120, collapsed: true, activeTab: 'done', opacity: 0.65 },
    storage,
  )

  assert.deepEqual(loadTodoPanelState(storage), {
    version: 1,
    x: 640,
    y: 120,
    collapsed: true,
    activeTab: 'done',
    opacity: 0.65,
  })
  assert.equal(
    parseTodoPanelState('{"version":1,"x":1,"y":2,"collapsed":false,"activeTab":"open"}')?.opacity,
    TODO_PANEL_DEFAULT_OPACITY,
  )
  assert.equal(
    parseTodoPanelState('{"version":1,"x":1,"y":2,"opacity":0.1}')?.opacity,
    0.5,
  )
  assert.equal(values.has(TODO_PANEL_STORAGE_KEY), true)
  assert.equal(parseTodoPanelState('{bad json'), null)
  assert.equal(parseTodoPanelState('{"version":2,"x":1,"y":2}'), null)
})
