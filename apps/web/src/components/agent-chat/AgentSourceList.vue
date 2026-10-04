<template>
  <section class="agent-source-list" aria-label="回答来源">
    <AgentDisclosureRow
      icon="file"
      label="来源"
      :count="sources.length"
      :open="expanded"
      @toggle="$emit('toggle')"
    />

    <div v-if="expanded" class="agent-source-list__items">
      <article
        v-for="(source, index) in sources"
        :key="source.key"
        class="agent-source-list__item"
        @click="$emit('open', source)"
      >
        <span class="agent-source-list__index">{{ index + 1 }}</span>
        <div class="agent-source-list__body">
          <strong>{{ source.title }}</strong>
          <span v-if="source.meta">{{ source.meta }}</span>
          <p v-if="source.preview">{{ source.preview }}</p>
        </div>
        <div class="agent-source-list__actions">
          <button
            v-if="source.kind === 'attachment'"
            type="button"
            @click.stop="$emit('open', source)"
          >
            预览
          </button>
          <button
            v-if="source.kind === 'attachment' && (source.conversation_id || source.conversation_name)"
            type="button"
            @click.stop="$emit('jump', source)"
          >
            <t-icon name="chat" />
            跳转
          </button>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import AgentDisclosureRow from './AgentDisclosureRow.vue'

export interface AgentSourceItem {
  key: string
  title: string
  meta: string
  preview?: string
  kind: 'attachment' | 'message' | 'source'
  resource_id?: string
  conversation_id?: string | number | null
  conversation_name?: string
  message_id?: string | number | null
  platform?: string
}

defineProps<{
  sources: AgentSourceItem[]
  expanded: boolean
}>()

defineEmits<{
  (event: 'toggle'): void
  (event: 'open', source: AgentSourceItem): void
  (event: 'jump', source: AgentSourceItem): void
}>()
</script>

<style scoped>
.agent-source-list {
  display: grid;
  gap: 8px;
  margin-top: 16px;
}

.agent-source-list__toggle {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  width: fit-content;
  min-width: 108px;
  gap: 14px;
  padding: 5px 7px;
  border: 1px solid var(--td-component-stroke);
  border-radius: 8px;
  color: var(--td-text-color-secondary);
  background: var(--td-bg-color-container);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: border-color 150ms ease, background 150ms ease, color 150ms ease;
}

.agent-source-list__toggle:hover {
  border-color: var(--td-brand-color-focus);
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-secondarycontainer);
}

.agent-source-list__toggle > span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.agent-source-list__toggle svg {
  width: 14px;
  height: 14px;
}

.agent-source-list__toggle small {
  min-width: 17px;
  padding: 1px 5px;
  border-radius: 9px;
  color: var(--td-text-color-placeholder);
  background: var(--td-bg-color-secondarycontainer);
  font-size: 10px;
  text-align: center;
}

.agent-source-list__items {
  display: grid;
  gap: 7px;
}

.agent-source-list__item {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  align-items: start;
  gap: 10px;
  padding: 11px 12px;
  border: 1px solid var(--td-component-stroke);
  border-radius: 10px;
  background: var(--td-bg-color-container);
  cursor: pointer;
  transition: border-color 150ms ease, background 150ms ease;
}

.agent-source-list__item:hover {
  border-color: var(--td-brand-color-focus);
  background: var(--td-bg-color-container-hover);
}

.agent-source-list__index {
  display: grid;
  width: 22px;
  height: 22px;
  place-items: center;
  border-radius: 7px;
  color: var(--td-brand-color);
  background: var(--td-brand-color-1);
  font-size: 10px;
  font-weight: 600;
}

.agent-source-list__body {
  min-width: 0;
}

.agent-source-list__body strong {
  display: block;
  overflow: hidden;
  color: var(--td-text-color-primary);
  font-size: 13px;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-source-list__body > span {
  display: block;
  margin-top: 3px;
  color: var(--td-text-color-placeholder);
  font-size: 11px;
}

.agent-source-list__body p {
  display: -webkit-box;
  margin: 7px 0 0;
  overflow: hidden;
  color: var(--td-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.agent-source-list__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.agent-source-list__actions button {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  gap: 4px;
  padding: 0 9px;
  border: 1px solid var(--td-component-stroke);
  border-radius: 7px;
  color: var(--td-text-color-secondary);
  background: var(--td-bg-color-container);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.agent-source-list__actions button:hover {
  border-color: var(--td-brand-color);
  color: var(--td-brand-color);
}

.agent-source-list__actions svg {
  width: 13px;
  height: 13px;
}

@media (max-width: 760px) {
  .agent-source-list__item {
    grid-template-columns: 22px minmax(0, 1fr);
  }

  .agent-source-list__actions {
    grid-column: 2;
    justify-content: flex-start;
  }
}
</style>
