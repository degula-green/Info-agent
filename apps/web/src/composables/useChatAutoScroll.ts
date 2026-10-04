import { nextTick, ref, type Ref } from 'vue'

export type ChatScrollMetrics = {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

export type ChatAutoScrollController = {
  hasNewContent: Ref<boolean>
  bind: (container: HTMLElement | null, content: HTMLElement | null) => void
  handleScroll: () => void
  jumpToLatest: (options?: { smooth?: boolean }) => Promise<void>
  reset: () => void
  scheduleFollow: () => void
  dispose: () => void
}

export function distanceFromBottom(metrics: ChatScrollMetrics): number {
  return Math.max(0, metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight)
}

export function isNearBottom(metrics: ChatScrollMetrics, threshold: number): boolean {
  return distanceFromBottom(metrics) <= threshold
}

function requestFrame(callback: () => void): number {
  if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
    return window.requestAnimationFrame(callback)
  }
  return window.setTimeout(callback, 16)
}

function cancelFrame(id: number): void {
  if (typeof window === 'undefined') return
  if (typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(id)
  else window.clearTimeout(id)
}

function currentTime(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

export function useChatAutoScroll(options: { bottomThreshold?: number } = {}): ChatAutoScrollController {
  const bottomThreshold = Math.max(1, options.bottomThreshold ?? 56)
  const hasNewContent = ref(false)

  let container: HTMLElement | null = null
  let content: HTMLElement | null = null
  let resizeObserver: ResizeObserver | null = null
  let followOutput = true
  let pendingFrame = 0
  let programmaticUntil = 0
  let disposed = false

  function metrics(): ChatScrollMetrics | null {
    if (!container) return null
    return {
      scrollTop: container.scrollTop,
      scrollHeight: container.scrollHeight,
      clientHeight: container.clientHeight,
    }
  }

  function scrollToLatest(smooth: boolean): void {
    if (!container) return
    programmaticUntil = currentTime() + (smooth ? 800 : 160)
    container.scrollTo({
      top: container.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto',
    })
  }

  function scheduleFollow(): void {
    if (disposed || !container || !followOutput || pendingFrame) return
    pendingFrame = requestFrame(() => {
      pendingFrame = 0
      if (followOutput && container) scrollToLatest(false)
    })
  }

  function handleContentResize(): void {
    if (!container || !content) return
    if (followOutput) {
      scheduleFollow()
      return
    }
    hasNewContent.value = true
  }

  function handleScroll(): void {
    if (!container || currentTime() < programmaticUntil) return
    const value = metrics()
    if (!value) return
    if (isNearBottom(value, bottomThreshold)) {
      followOutput = true
      hasNewContent.value = false
      return
    }
    followOutput = false
  }

  function handleUserIntent(): void {
    programmaticUntil = 0
  }

  function teardownBindings(): void {
    if (pendingFrame) {
      cancelFrame(pendingFrame)
      pendingFrame = 0
    }
    resizeObserver?.disconnect()
    resizeObserver = null
    if (container) {
      container.removeEventListener('scroll', handleScroll)
      container.removeEventListener('wheel', handleUserIntent)
      container.removeEventListener('touchstart', handleUserIntent)
      container.removeEventListener('pointerdown', handleUserIntent)
    }
    container = null
    content = null
  }

  function bind(nextContainer: HTMLElement | null, nextContent: HTMLElement | null): void {
    if (disposed || (container === nextContainer && content === nextContent)) return
    teardownBindings()
    container = nextContainer
    content = nextContent
    if (!container || !content) return

    container.addEventListener('scroll', handleScroll, { passive: true })
    container.addEventListener('wheel', handleUserIntent, { passive: true })
    container.addEventListener('touchstart', handleUserIntent, { passive: true })
    container.addEventListener('pointerdown', handleUserIntent, { passive: true })

    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(handleContentResize)
      resizeObserver.observe(content)
    }
  }

  async function jumpToLatest(options: { smooth?: boolean } = {}): Promise<void> {
    if (disposed) return
    followOutput = true
    hasNewContent.value = false
    await nextTick()
    if (!container) return
    if (pendingFrame) {
      cancelFrame(pendingFrame)
      pendingFrame = 0
    }
    await new Promise<void>((resolve) => {
      pendingFrame = requestFrame(() => {
        pendingFrame = 0
        resolve()
      })
    })
    if (container) scrollToLatest(Boolean(options.smooth))
  }

  function reset(): void {
    followOutput = true
    hasNewContent.value = false
    programmaticUntil = 0
    if (pendingFrame) {
      cancelFrame(pendingFrame)
      pendingFrame = 0
    }
  }

  function dispose(): void {
    disposed = true
    teardownBindings()
  }

  return {
    hasNewContent,
    bind,
    handleScroll,
    jumpToLatest,
    reset,
    scheduleFollow,
    dispose,
  }
}
