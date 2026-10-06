<template>
  <t-dialog
    :visible="visible"
    :header="false"
    :footer="false"
    :close-btn="false"
    width="min(92vw, 1720px)"
    dialog-class-name="info-search-preview-dialog"
    placement="center"
    destroy-on-close
    @update:visible="emit('update:visible', $event)"
  >
    <article v-if="file" class="search-preview" aria-labelledby="search-preview-title">
      <header class="search-preview__header">
        <div>
          <h2 id="search-preview-title">{{ file.name }}</h2>
          <p>{{ file.uploader || '未知发送人' }} · {{ file.time || '发送时间未知' }}</p>
        </div>
        <button type="button" class="search-preview__close" aria-label="关闭预览" @click="emit('update:visible', false)"><t-icon name="close" /></button>
      </header>
      <main class="search-preview__body"><InfoAttachmentPreview :file="file" :active="visible" /></main>
      <footer class="search-preview__footer">
        <span>{{ file.contentAccessRequired ? '受保护文件仅提供元数据' : '来自知识库检索结果' }}</span>
        <div>
          <t-button v-if="result?.conversationId" variant="outline" @click="emit('open-source')"><template #icon><t-icon name="chat" /></template>查看来源会话</t-button>
          <t-button variant="outline" :loading="downloading" :disabled="file.contentAccessRequired" @click="emit('download')"><template #icon><t-icon :name="file.contentAccessRequired ? 'lock-on' : 'download'" /></template>下载</t-button>
        </div>
      </footer>
    </article>
  </t-dialog>
</template>

<script setup lang="ts">
import InfoAttachmentPreview from '@/components/InfoAttachmentPreview.vue'
import type { InfoFile, SearchResult } from '@/mock'

defineProps<{
  visible: boolean
  result: SearchResult | null
  file: InfoFile | null
  downloading?: boolean
}>()

const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'open-source'): void
  (event: 'download'): void
}>()
</script>

<style scoped lang="less">
.search-preview { display: flex; height: min(84vh, 900px); min-height: 520px; flex-direction: column; overflow: hidden; background: var(--td-bg-color-container); }.search-preview__header { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 20px 24px 16px; border-bottom: 1px solid var(--td-component-stroke); }.search-preview__header h2 { margin: 0; overflow: hidden; color: var(--td-text-color-primary); font-size: 18px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }.search-preview__header p { margin: 6px 0 0; color: var(--td-text-color-secondary); font-size: 12px; }.search-preview__close { display: inline-grid; width: 34px; height: 34px; flex: 0 0 34px; place-items: center; padding: 0; border: 0; border-radius: 6px; color: var(--td-text-color-secondary); background: transparent; cursor: pointer; }.search-preview__close:hover { color: var(--td-text-color-primary); background: var(--td-bg-color-secondarycontainer); }.search-preview__body { display: flex; min-height: 0; flex: 1; padding: 14px; overflow: hidden; }.search-preview__footer { display: flex; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 18px; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-secondary); font-size: 12px; }.search-preview__footer > div { display: flex; gap: 8px; }
</style>

<style lang="less">
.info-search-preview-dialog .t-dialog { overflow: hidden; padding: 0; border-radius: 12px; }
.info-search-preview-dialog .t-dialog__body { padding: 0; }
.t-dialog__wrap:has(.info-search-preview-dialog) .t-dialog__position { display: flex; min-height: 100%; height: 100%; align-items: center; justify-content: center; box-sizing: border-box; }
</style>
