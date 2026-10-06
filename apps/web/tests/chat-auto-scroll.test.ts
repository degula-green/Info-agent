import assert from 'node:assert/strict'
import test from 'node:test'
import { distanceFromBottom, isNearBottom } from '../src/composables/useChatAutoScroll.ts'

test('distance from bottom never becomes negative after overscroll', () => {
  assert.equal(distanceFromBottom({ scrollTop: 900, scrollHeight: 500, clientHeight: 400 }), 0)
})

test('the follow threshold distinguishes reading from staying at the bottom', () => {
  const near = { scrollTop: 640, scrollHeight: 1000, clientHeight: 320 }
  const away = { scrollTop: 420, scrollHeight: 1000, clientHeight: 320 }

  assert.equal(isNearBottom(near, 56), true)
  assert.equal(isNearBottom(away, 56), false)
  assert.equal(distanceFromBottom(away), 260)
})
