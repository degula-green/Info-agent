<template>
  <section class="agent-steps-timeline">
    <AgentDisclosureRow
      icon="time"
      label="执行过程"
      :count="steps.length"
      :open="open"
      @toggle="$emit('toggle')"
    />

    <ol v-if="open" class="agent-steps-timeline__list">
      <li
        v-for="step in steps"
        :key="step.id"
        class="agent-steps-timeline__item"
        :class="`agent-steps-timeline__item--${step.status}`"
      >
        <span class="agent-steps-timeline__marker" aria-hidden="true">
          <t-icon :name="stepIcon(step.status)" />
        </span>
        <span class="agent-steps-timeline__body">
          <strong>{{ displayStepLabel(step.label) }}</strong>
          <span
            v-for="(stage, index) in step.stages || []"
            :key="`${step.id}-${index}`"
            class="agent-steps-timeline__stage"
          >
            {{ stage }}
          </span>
        </span>
        <small>{{ stepStatusLabel(step.status) }}</small>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import AgentDisclosureRow from './AgentDisclosureRow.vue'

defineProps<{
  steps: Array<{ id: string; label: string; status: string; stages?: string[] }>
  open: boolean
}>()

defineEmits<{
  (event: 'toggle'): void
}>()

function displayStepLabel(label: string): string {
  return ({
    'form.preview': '阅读表单并预填',
    'form.apply': '写入表格',
    'knowledge.search_sources': '检索本地知识',
    'knowledge.search_content': '查找相关内容',
    'knowledge.answer': '整理回答',
    'web.fetch': '读取网页',
    'web.extract': '提取网页内容',
    'answer.compose': '整理回答',
    'todo.create': '创建待办',
  } as Record<string, string>)[label] || label
}

function stepStatusLabel(status: string): string {
  return ({
    pending: '等待执行',
    ready: '准备执行',
    running: '进行中',
    waiting_approval: '等待确认',
    succeeded: '已完成',
    failed: '失败',
    skipped: '已跳过',
  } as Record<string, string>)[status] || status
}

function stepIcon(status: string): string {
  if (status === 'succeeded') return 'check'
  if (status === 'failed') return 'close'
  if (status === 'running') return 'loading'
  return 'time'
}
</script>

<style scoped>
.agent-steps-timeline {
  margin-top: 2px;
}

.agent-steps-timeline__toggle {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  min-width: 150px;
  gap: 12px;
  padding: 5px 7px 5px 0;
  border: 0;
  color: var(--td-text-color-secondary);
  background: transparent;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.agent-steps-timeline__toggle:hover {
  color: var(--td-text-color-primary);
}

.agent-steps-timeline__title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.agent-steps-timeline__title svg {
  width: 14px;
  height: 14px;
}

.agent-steps-timeline__title small {
  min-width: 17px;
  padding: 1px 5px;
  border-radius: 9px;
  color: var(--td-text-color-placeholder);
  background: var(--td-bg-color-secondarycontainer);
  font-size: 10px;
  text-align: center;
}

.agent-steps-timeline__list {
  display: grid;
  gap: 0;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.agent-steps-timeline__item {
  position: relative;
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  align-items: start;
  gap: 10px;
  padding: 7px 0 7px 3px;
}

.agent-steps-timeline__item:not(:last-child)::before {
  position: absolute;
  top: 29px;
  bottom: -3px;
  left: 13px;
  width: 1px;
  background: var(--td-component-stroke);
  content: '';
}

.agent-steps-timeline__marker {
  position: relative;
  z-index: 1;
  display: grid;
  width: 21px;
  height: 21px;
  place-items: center;
  border: 1px solid var(--td-component-stroke);
  border-radius: 50%;
  color: var(--td-text-color-placeholder);
  background: var(--td-bg-color-container);
}

.agent-steps-timeline__marker svg {
  width: 12px;
  height: 12px;
}

.agent-steps-timeline__item--succeeded .agent-steps-timeline__marker {
  border-color: var(--td-success-color-3);
  color: var(--td-success-color);
}

.agent-steps-timeline__item--failed .agent-steps-timeline__marker {
  border-color: var(--td-error-color-3);
  color: var(--td-error-color);
}

.agent-steps-timeline__item--running .agent-steps-timeline__marker {
  border-color: var(--td-brand-color-focus);
  color: var(--td-brand-color);
}

.agent-steps-timeline__item--running .agent-steps-timeline__marker svg {
  animation: agent-step-spin 1.2s linear infinite;
}

.agent-steps-timeline__body {
  display: grid;
  min-width: 0;
  gap: 3px;
  padding-top: 1px;
}

.agent-steps-timeline__body strong {
  color: var(--td-text-color-primary);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.45;
}

.agent-steps-timeline__stage {
  color: var(--td-text-color-placeholder);
  font-size: 11px;
  line-height: 1.45;
}

.agent-steps-timeline__item > small {
  padding-top: 2px;
  color: var(--td-text-color-placeholder);
  font-size: 11px;
  white-space: nowrap;
}

@keyframes agent-step-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-steps-timeline__item--running .agent-steps-timeline__marker svg {
    animation: none;
  }
}
</style>
