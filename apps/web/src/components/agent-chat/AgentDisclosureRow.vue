<template>
  <button
    type="button"
    class="agent-disclosure-row"
    :aria-expanded="open"
    @click="$emit('toggle')"
  >
    <span class="agent-disclosure-row__leading">
      <t-icon :name="icon" />
      <span>{{ label }}</span>
      <small v-if="count">{{ count }}</small>
    </span>
    <t-icon :name="open ? 'chevron-up' : 'chevron-down'" />
  </button>
</template>

<script setup lang="ts">
defineProps<{
  icon: string
  label: string
  count?: number
  open: boolean
}>()

defineEmits<{
  (event: 'toggle'): void
}>()
</script>

<style scoped>
.agent-disclosure-row {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  width: fit-content;
  min-width: 126px;
  gap: 18px;
  padding: 5px 2px;
  border: 0;
  color: var(--td-text-color-secondary);
  background: transparent;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: color 150ms ease;
}

.agent-disclosure-row:hover {
  color: var(--td-text-color-primary);
}

.agent-disclosure-row__leading {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.agent-disclosure-row svg {
  width: 14px;
  height: 14px;
}

.agent-disclosure-row small {
  min-width: 17px;
  padding: 1px 5px;
  border-radius: 9px;
  color: var(--td-text-color-placeholder);
  background: var(--td-bg-color-secondarycontainer);
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}
</style>
