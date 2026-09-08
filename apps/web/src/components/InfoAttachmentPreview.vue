<template>
  <section class="attachment-preview" aria-label="附件预览">
    <div v-if="file.contentAccessRequired" class="attachment-preview__state"><t-icon name="lock-on" />受保护附件仅提供元数据</div>
    <div v-else-if="loading" class="attachment-preview__state"><t-icon name="loading" />正在加载预览…</div>
    <div v-else-if="error" class="attachment-preview__state attachment-preview__state--error"><t-icon name="error-circle" />{{ error }}</div>
    <template v-else>
      <img v-if="isImage" :src="blobUrl" :alt="file.name" />
      <iframe v-else-if="isPdf" :src="blobUrl" :title="file.name" />
      <pre v-else>{{ textContent || file.content || '暂无可预览内容' }}</pre>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import type { InfoFile } from '@/mock'
import { getKnowledgeAttachmentContent } from '@/api/info-knowledge'
const props = defineProps<{ file: InfoFile; active: boolean }>()
const loading = ref(false); const error = ref(''); const textContent = ref(''); const blobUrl = ref('')
const extension = () => String(props.file.type || props.file.name.split('.').pop() || '').toLowerCase().replace(/^\./, '')
const isImage = ref(false); const isPdf = ref(false)
function release() { if (blobUrl.value) URL.revokeObjectURL(blobUrl.value); blobUrl.value = '' }
async function load() { release(); textContent.value = ''; error.value = ''; loading.value = false; if (!props.active || props.file.contentAccessRequired) return; loading.value = true; const ext = extension(); isImage.value = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext); isPdf.value = ext === 'pdf'; try { const blob = await getKnowledgeAttachmentContent(props.file.id); if (isImage.value || isPdf.value) blobUrl.value = URL.createObjectURL(blob); else textContent.value = await blob.text() } catch { error.value = '附件预览暂不可用' } finally { loading.value = false } }
watch(() => [props.active, props.file.id, props.file.type, props.file.contentAccessRequired], load, { immediate: true })
onUnmounted(release)
</script>

<style scoped>
.attachment-preview { min-height: 220px; padding: 16px; border-radius: 8px; background: var(--td-bg-color-secondarycontainer); }.attachment-preview__state { display: grid; min-height: 180px; place-items: center; gap: 8px; color: var(--td-text-color-secondary); }.attachment-preview__state--error { color: var(--td-error-color); }.attachment-preview img { display: block; max-width: 100%; max-height: 420px; margin: auto; }.attachment-preview iframe { width: 100%; height: 420px; border: 0; }.attachment-preview pre { max-height: 420px; margin: 0; overflow: auto; white-space: pre-wrap; color: var(--td-text-color-secondary); font: 13px/1.7 var(--app-font-family); }
</style>
