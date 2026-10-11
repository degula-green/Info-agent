import assert from 'node:assert/strict'
import test from 'node:test'
import {
  deletionHasError,
  deletionStatusLabel,
  deletionStatusTheme,
  matchesAccessView,
  matchesDeletionView,
} from '../src/access-control/access-display.ts'

test('deletion display collapses execution substates to deleting and completed', () => {
  assert.equal(deletionStatusLabel('pending'), '待审批')
  assert.equal(deletionStatusLabel('approved'), '删除中')
  assert.equal(deletionStatusLabel('executing'), '删除中')
  assert.equal(deletionStatusLabel('failed'), '删除中')
  assert.equal(deletionStatusLabel('completed'), '删除完成')
  assert.equal(deletionStatusLabel('rejected'), '已拒绝')
  assert.equal(deletionStatusTheme('executing'), 'primary')
  assert.equal(deletionStatusTheme('completed'), 'success')
})

test('deletion errors do not introduce a third user-facing status', () => {
  assert.equal(deletionHasError('executing', true), true)
  assert.equal(deletionHasError('failed', false), true)
  assert.equal(deletionStatusLabel('failed'), '删除中')
})

test('access views keep pending, active, and terminal requests separate', () => {
  assert.equal(matchesAccessView('pending', 'inbox'), true)
  assert.equal(matchesAccessView('approved', 'mine'), true)
  assert.equal(matchesAccessView('executing', 'mine'), true)
  assert.equal(matchesAccessView('completed', 'history'), true)
  assert.equal(matchesAccessView('failed', 'history'), true)
  assert.equal(matchesAccessView('pending', 'history'), false)
})

test('failed deletions remain active because the deletion is still in progress', () => {
  assert.equal(matchesDeletionView('failed', 'mine'), true)
  assert.equal(matchesDeletionView('approved', 'mine'), true)
  assert.equal(matchesDeletionView('completed', 'history'), true)
  assert.equal(matchesDeletionView('rejected', 'history'), true)
  assert.equal(matchesDeletionView('failed', 'history'), false)
})
