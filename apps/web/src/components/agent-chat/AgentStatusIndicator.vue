<template>
  <span class="agent-status-indicator" :class="`agent-status-indicator--${status || 'running'}`">
    <span class="agent-status-indicator__dot" aria-hidden="true" />
    <span>{{ text }}</span>
  </span>
</template>

<script setup lang="ts">
defineProps<{
  status?: string
  text: string
}>()
</script>

<style scoped>
.agent-status-indicator {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--td-text-color-secondary);
  font-size: 12px;
  line-height: 20px;
}

.agent-status-indicator__dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--td-brand-color);
}

.agent-status-indicator--succeeded .agent-status-indicator__dot,
.agent-status-indicator--ready .agent-status-indicator__dot {
  background: var(--td-success-color);
}

.agent-status-indicator--failed .agent-status-indicator__dot,
.agent-status-indicator--unknown .agent-status-indicator__dot {
  background: var(--td-error-color);
}

.agent-status-indicator--waiting_approval .agent-status-indicator__dot,
.agent-status-indicator--waiting_input .agent-status-indicator__dot {
  background: var(--td-warning-color);
}

.agent-status-indicator--cancelled .agent-status-indicator__dot {
  background: var(--td-text-color-placeholder);
}

.agent-status-indicator--received .agent-status-indicator__dot,
.agent-status-indicator--planning .agent-status-indicator__dot,
.agent-status-indicator--executing .agent-status-indicator__dot {
  animation: agent-status-pulse 1.5s ease-in-out infinite;
}

@keyframes agent-status-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }

  50% {
    opacity: 0.55;
    transform: scale(0.82);
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-status-indicator__dot {
    animation: none !important;
  }
}
</style>
