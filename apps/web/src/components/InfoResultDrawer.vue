<template>
  <t-drawer v-model:visible="open" :header="result?.title || '检索结果'" size="520px" :footer="false" destroy-on-close>
    <div v-if="result" class="result-drawer">
      <div class="result-drawer__meta"><t-tag :theme="result.kind === 'file' ? 'warning' : result.kind === 'qa' ? 'success' : 'primary'" variant="light">{{ result.kind === 'file' ? '文件' : result.kind === 'qa' ? '历史问答' : '原始消息' }}</t-tag><span>{{ result.subtitle }}</span></div>
      <div v-if="result.kind === 'message'" class="result-drawer__facts"><span><b>来源平台</b>{{ result.source }}</span><span><b>来源会话</b>{{ result.subtitle.split(' · ')[1] || '—' }}</span><span><b>发送人 / 时间</b>{{ result.sender || '—' }} · {{ result.time || '—' }}</span></div>
      <div v-if="result.kind === 'file'" class="result-drawer__file"><t-icon name="file" /><div><strong>{{ result.title }}</strong><small>{{ result.source }} · {{ result.uploader || '—' }} · {{ result.time || '—' }}</small></div></div>
      <div v-if="result.kind === 'message' && result.context?.length" class="result-drawer__context"><div class="result-drawer__context-title">上下文消息</div><div v-for="item in result.context" :key="item.id" class="result-drawer__context-item"><strong>{{ item.sender }}</strong><span>{{ item.content }}</span><time>{{ item.time }}</time></div></div>
      <t-textarea v-model="draft" :readonly="!editing" :autosize="{ minRows: 10, maxRows: 24 }" :placeholder="result.kind === 'qa' ? '问答内容' : '检索内容'" />
      <div class="result-drawer__actions"><t-button v-if="result.kind !== 'qa' && !editing" variant="outline" @click="editing = true"><template #icon><t-icon name="edit" /></template>编辑</t-button><t-button v-else-if="result.kind !== 'qa'" theme="primary" @click="save"><template #icon><t-icon name="check" /></template>保存</t-button><t-button variant="outline" @click="$emit('toast', '已生成下载文件（原型演示）')"><template #icon><t-icon name="download" /></template>下载</t-button></div>
    </div>
  </t-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { SearchResult } from '../mock'

const props = defineProps<{ visible: boolean; result: SearchResult | null }>()
const emit = defineEmits<{ (event: 'update:visible', value: boolean): void; (event: 'toast', text: string): void; (event: 'save', result: SearchResult, draft: string): void }>()
const open = computed({ get: () => props.visible, set: (value: boolean) => emit('update:visible', value) }); const draft = ref(''); const editing = ref(false)
watch(() => props.result, (result) => { draft.value = result?.content || result?.subtitle || ''; editing.value = false }, { immediate: true })
function save() { editing.value = false; if (props.result) emit('save', props.result, draft.value); emit('toast', '内容已保存到本地 Mock 数据') }
</script>

<style lang="less" scoped>
.result-drawer { padding: 2px 0 8px; }.result-drawer__meta { display: flex; align-items: flex-start; gap: 9px; margin-bottom: 18px; color: var(--td-text-color-secondary); font-size: 11px; line-height: 1.5; }.result-drawer__facts { display: grid; gap: 8px; margin-bottom: 15px; padding: 12px 13px; border: 1px solid var(--td-component-stroke); border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }.result-drawer__facts span { display: flex; gap: 10px; color: var(--td-text-color-secondary); font-size: 12px; }.result-drawer__facts b { width: 82px; color: var(--td-text-color-placeholder); font-weight: 400; }.result-drawer__file { display: flex; align-items: center; gap: 10px; margin-bottom: 15px; padding: 13px; border: 1px solid var(--td-component-stroke); border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }.result-drawer__file > svg { width: 24px; color: var(--td-warning-color); }.result-drawer__file strong, .result-drawer__file small { display: block; }.result-drawer__file strong { font-size: 13px; }.result-drawer__file small { margin-top: 4px; color: var(--td-text-color-secondary); font-size: 11px; }.result-drawer__context { margin-bottom: 15px; padding: 10px 12px; border: 1px solid var(--td-component-stroke); border-radius: 6px; }.result-drawer__context-title { margin-bottom: 8px; color: var(--td-text-color-placeholder); font-size: 11px; }.result-drawer__context-item { display: grid; grid-template-columns: 58px 1fr auto; gap: 7px; align-items: baseline; padding: 6px 0; border-top: 1px solid var(--td-component-stroke); font-size: 11px; }.result-drawer__context-item:first-of-type { border-top: 0; }.result-drawer__context-item strong { color: var(--td-brand-color); font-weight: 500; }.result-drawer__context-item span { color: var(--td-text-color-secondary); line-height: 1.5; }.result-drawer__context-item time { color: var(--td-text-color-placeholder); font-size: 10px; }.result-drawer__actions { display: flex; gap: 8px; margin-top: 15px; }
</style>
