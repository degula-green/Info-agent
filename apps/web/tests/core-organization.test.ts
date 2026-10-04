import assert from 'node:assert/strict'
import { test } from 'node:test'
import { normalizeOrganizationExitPreflight } from '../src/api/core-organization.ts'

test('organization exit preflight normalizes null arrays', () => {
  const result = normalizeOrganizationExitPreflight({
    allowed: true,
    blockers: null as unknown as string[],
    warnings: null as unknown as string[],
  })
  assert.equal(result.allowed, true)
  assert.deepEqual(result.blockers, [])
  assert.deepEqual(result.warnings, [])
})
