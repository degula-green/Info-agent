export type AuthSessionExpiredReason = 'expired' | 'invalid' | 'refresh_failed'

export type AuthSessionExpiredEvent = {
  reason: AuthSessionExpiredReason
  occurredAt: number
}

type Listener = (event: AuthSessionExpiredEvent) => void

const listeners = new Set<Listener>()
let lastEmittedAt = 0

export function emitAuthSessionExpired(reason: AuthSessionExpiredReason) {
  const now = Date.now()
  if (now - lastEmittedAt < 1000) return
  lastEmittedAt = now
  const event = { reason, occurredAt: now }
  for (const listener of listeners) listener(event)
}

export function subscribeAuthSessionExpired(listener: Listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
