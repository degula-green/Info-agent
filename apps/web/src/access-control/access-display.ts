export type AccessView = 'mine' | 'inbox' | 'history'
export type AccessStatusTheme = 'default' | 'primary' | 'success' | 'warning' | 'danger'

export function matchesAccessView(status: string, view: AccessView) {
  if (view === 'inbox') return status === 'pending'
  if (view === 'history') {
    return ['cancelled', 'completed', 'failed', 'rejected', 'superseded', 'expired', 'revoked'].includes(status)
  }
  return ['pending', 'approved', 'executing'].includes(status)
}

export function accessStatusLabel(status: string) {
  return ({
    pending: '待审批',
    approved: '已通过',
    executing: '处理中',
    completed: '已完成',
    rejected: '已拒绝',
    failed: '执行失败',
    cancelled: '已关闭',
    superseded: '已合并',
    expired: '已过期',
    revoked: '已撤销',
  } as Record<string, string>)[status] || status
}

export function accessStatusTheme(status: string): AccessStatusTheme {
  if (status === 'approved' || status === 'completed') return 'success'
  if (status === 'rejected' || status === 'failed') return 'danger'
  if (status === 'pending') return 'warning'
  if (status === 'executing') return 'primary'
  return 'default'
}

// Deletion requests retain their internal execution states for retries and
// auditing, but the UI intentionally exposes only the coarse lifecycle.
export function deletionStatusLabel(status: string) {
  if (status === 'completed') return '删除完成'
  if (status === 'rejected') return '已拒绝'
  if (status === 'cancelled' || status === 'superseded' || status === 'expired' || status === 'revoked') {
    return accessStatusLabel(status)
  }
  if (status === 'pending') return '待审批'
  return '删除中'
}

export function deletionStatusTheme(status: string): AccessStatusTheme {
  if (status === 'completed') return 'success'
  if (status === 'rejected') return 'danger'
  if (status === 'pending') return 'warning'
  if (status === 'cancelled' || status === 'superseded' || status === 'expired' || status === 'revoked') {
    return 'default'
  }
  return 'primary'
}

export function deletionHasError(status: string, itemHasError = false) {
  return itemHasError || status === 'failed'
}

export function matchesDeletionView(status: string, view: AccessView) {
  if (view === 'inbox') return status === 'pending'
  if (view === 'history') {
    return ['cancelled', 'completed', 'rejected', 'superseded', 'expired', 'revoked'].includes(status)
  }
  return !['cancelled', 'completed', 'rejected', 'superseded', 'expired', 'revoked'].includes(status)
}
