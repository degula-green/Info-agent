<template>
  <div class="agent-answer-content" v-html="html" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderChatMarkdown } from '@/utils/chatMarkdownRenderer'

const props = defineProps<{
  content: string
}>()

const html = computed(() => renderChatMarkdown(props.content))
</script>

<style scoped>
.agent-answer-content {
  color: var(--td-text-color-primary);
  font-size: 15px;
  line-height: 1.85;
  overflow-wrap: anywhere;
}

.agent-answer-content :deep(p) {
  margin: 0 0 14px;
}

.agent-answer-content :deep(p:last-child) {
  margin-bottom: 0;
}

.agent-answer-content :deep(h1),
.agent-answer-content :deep(h2),
.agent-answer-content :deep(h3) {
  margin: 24px 0 10px;
  color: var(--td-text-color-primary);
  line-height: 1.35;
}

.agent-answer-content :deep(h1:first-child),
.agent-answer-content :deep(h2:first-child),
.agent-answer-content :deep(h3:first-child) {
  margin-top: 0;
}

.agent-answer-content :deep(h1) {
  font-size: 21px;
}

.agent-answer-content :deep(h2) {
  font-size: 18px;
}

.agent-answer-content :deep(h3) {
  font-size: 16px;
}

.agent-answer-content :deep(ul),
.agent-answer-content :deep(ol) {
  margin: 8px 0 14px;
  padding-left: 22px;
}

.agent-answer-content :deep(li) {
  margin: 4px 0;
}

.agent-answer-content :deep(blockquote) {
  margin: 14px 0;
  padding: 9px 13px;
  border-left: 1px solid var(--td-brand-color);
  border-radius: 0 8px 8px 0;
  color: var(--td-text-color-secondary);
  background: var(--td-bg-color-secondarycontainer);
}

.agent-answer-content :deep(code) {
  padding: 2px 5px;
  border-radius: 5px;
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-secondarycontainer);
  font-family: var(--app-font-family-mono, ui-monospace, monospace);
  font-size: 0.92em;
}

.agent-answer-content :deep(pre) {
  margin: 14px 0;
  padding: 14px 16px;
  overflow-x: auto;
  border: 1px solid var(--td-component-stroke);
  border-radius: 10px;
  background: var(--td-bg-color-secondarycontainer);
}

.agent-answer-content :deep(pre code) {
  padding: 0;
  background: transparent;
}

.agent-answer-content :deep(a) {
  color: var(--td-brand-color);
  text-decoration: none;
}

.agent-answer-content :deep(a:hover) {
  text-decoration: underline;
}

.agent-answer-content :deep(.chat-table-scroll) {
  margin: 14px 0;
  overflow-x: auto;
  border: 1px solid var(--td-component-stroke);
  border-radius: 10px;
}

.agent-answer-content :deep(table) {
  width: 100%;
  min-width: 520px;
  border-collapse: collapse;
  font-size: 13px;
}

.agent-answer-content :deep(th),
.agent-answer-content :deep(td) {
  padding: 9px 11px;
  border-bottom: 1px solid var(--td-component-stroke);
  color: var(--td-text-color-secondary);
  text-align: left;
  vertical-align: top;
}

.agent-answer-content :deep(th) {
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-secondarycontainer);
  font-weight: 600;
}

.agent-answer-content :deep(tr:last-child td) {
  border-bottom: 0;
}

@media (max-width: 760px) {
  .agent-answer-content {
    font-size: 14px;
    line-height: 1.8;
  }

  .agent-answer-content :deep(h1) {
    font-size: 19px;
  }

  .agent-answer-content :deep(h2) {
    font-size: 17px;
  }
}
</style>
