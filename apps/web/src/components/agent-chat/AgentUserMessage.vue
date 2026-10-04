<template>
  <article class="agent-user-message">
    <div class="agent-user-message__bubble">{{ text }}</div>
    <div class="agent-user-message__actions" aria-label="问题操作">
      <button type="button" title="复制问题" aria-label="复制问题" @click="$emit('copy')">
        <t-icon name="file-copy" />
      </button>
      <button type="button" title="编辑问题" aria-label="编辑问题" @click="$emit('edit')">
        <t-icon name="edit-1" />
      </button>
    </div>
  </article>
</template>

<script setup lang="ts">
defineProps<{
  text: string
}>()

defineEmits<{
  (event: 'copy'): void
  (event: 'edit'): void
}>()
</script>

<style scoped>
.agent-user-message {
  position: relative;
  display: flex;
  width: min(640px, 78%);
  margin-left: auto;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

.agent-user-message__bubble {
  max-width: 100%;
  padding: 12px 16px;
  border: 1px solid var(--td-component-stroke);
  border-radius: 18px;
  background: var(--td-bg-color-secondarycontainer);
  color: var(--td-text-color-primary);
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.agent-user-message__actions {
  display: flex;
  flex-direction: row;
  align-items: center;
  align-self: flex-end;
  gap: 4px;
  margin: 0;
  opacity: 0;
  pointer-events: none;
  transition: opacity 160ms ease;
}

.agent-user-message:hover .agent-user-message__actions,
.agent-user-message:focus-within .agent-user-message__actions {
  opacity: 1;
  pointer-events: auto;
}

.agent-user-message__actions button {
  display: inline-grid;
  width: 28px;
  height: 28px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 7px;
  color: var(--td-text-color-placeholder);
  background: transparent;
  cursor: pointer;
}

.agent-user-message__actions button:hover {
  color: var(--td-text-color-secondary);
  background: var(--td-bg-color-secondarycontainer);
}

.agent-user-message__actions svg {
  width: 16px;
  height: 16px;
}

@media (hover: none) {
  .agent-user-message__actions {
    opacity: 1;
    pointer-events: auto;
  }
}

@media (max-width: 760px) {
  .agent-user-message {
    width: min(90%, 640px);
  }

  .agent-user-message__bubble {
    padding: 10px 13px;
    border-radius: 16px;
    font-size: 13px;
  }

}
</style>
