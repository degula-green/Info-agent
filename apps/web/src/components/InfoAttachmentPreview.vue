<template>
  <section class="attachment-preview" aria-label="附件预览">
    <div v-if="file.contentAccessRequired" class="attachment-preview__state"><t-icon name="lock-on" />受保护附件仅提供元数据</div>
    <div v-else-if="oversizedPresentation" class="attachment-preview__state attachment-preview__state--large"><t-icon name="file" /><strong>文件过大，暂不在浏览器中直接预览</strong><span>{{ file.size || '大型演示文稿' }} 在浏览器中完整解析会占用大量内存，请下载原文件查看。</span><t-button theme="primary" variant="outline" :loading="downloading" @click="downloadOriginal">下载原文件</t-button></div>
    <div v-else-if="loading" class="attachment-preview__state"><t-icon name="loading" />正在加载预览…</div>
    <div v-else-if="error" class="attachment-preview__state attachment-preview__state--error"><t-icon name="error-circle" />{{ error }}</div>
    <template v-else>
      <div v-if="isImage" class="attachment-preview__image-box"><img :src="blobUrl" :alt="file.name" /></div>
      <div v-else-if="isPdf" class="attachment-preview__pdf" aria-label="PDF 只读预览">
        <canvas v-for="page in pdfPages" :key="page" :ref="(element) => setPdfCanvas(element, page)" class="attachment-preview__pdf-page" />
      </div>
      <div v-else-if="isWord" ref="wordContainer" class="attachment-preview__word attachment-preview__source" aria-label="Word 源文件只读预览" />
      <div v-else-if="isSpreadsheet" ref="spreadsheetContainer" class="attachment-preview__spreadsheet attachment-preview__source" aria-label="Excel 源文件只读预览" />
      <div v-else-if="isPresentation" ref="presentationContainer" class="attachment-preview__presentation attachment-preview__source" aria-label="PowerPoint 源文件只读预览" />
      <pre v-else-if="isText">{{ textContent || file.content || '暂无可预览内容' }}</pre>
      <div v-else class="attachment-preview__unsupported"><t-icon name="file" /><strong>暂不支持此格式的在线预览</strong><span>{{ file.name }}</span></div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch, type ComponentPublicInstance } from 'vue'
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy } from 'pdfjs-dist/legacy/build/pdf.mjs'
import pdfWorker from 'pdfjs-dist/legacy/build/pdf.worker.mjs?url'
import type { InfoFile } from '@/mock'
import { getKnowledgeAttachmentContent } from '@/api/info-knowledge'

GlobalWorkerOptions.workerSrc = pdfWorker
const props = defineProps<{ file: InfoFile; active: boolean }>()
const loading = ref(false); const error = ref(''); const textContent = ref(''); const blobUrl = ref('')
const downloading = ref(false)
const wordContainer = ref<HTMLElement | null>(null); const spreadsheetContainer = ref<HTMLElement | null>(null); const presentationContainer = ref<HTMLElement | null>(null)
const isImage = ref(false); const isPdf = ref(false); const isWord = ref(false); const isSpreadsheet = ref(false); const isPresentation = ref(false); const isText = ref(false)
const oversizedPresentation = ref(false)
const pdfPages = ref<number[]>([]); const pdfCanvases = new Map<number, HTMLCanvasElement>(); let pdfDocument: PDFDocumentProxy | null = null; let loadVersion = 0
const extension = () => {
  const nameExtension = String(props.file.name.split('.').pop() || '').toLowerCase().replace(/^\./, '')
  if (nameExtension && nameExtension !== props.file.name.toLowerCase()) return nameExtension
  return String(props.file.type || props.file.mimeType || '').toLowerCase().replace(/^\./, '')
}
function release() { if (blobUrl.value) URL.revokeObjectURL(blobUrl.value); blobUrl.value = ''; pdfDocument?.cleanup(); pdfDocument = null; pdfPages.value = []; pdfCanvases.clear(); wordContainer.value?.replaceChildren(); spreadsheetContainer.value?.replaceChildren(); presentationContainer.value?.replaceChildren() }
function setPdfCanvas(element: Element | ComponentPublicInstance | null, page: number) { if (element instanceof HTMLCanvasElement) pdfCanvases.set(page, element) }
async function downloadOriginal() {
  if (downloading.value) return
  downloading.value = true
  try {
    const blob = await getKnowledgeAttachmentContent(props.file.id, true)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = props.file.name || 'attachment'
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch {
    error.value = '原文件下载失败，请稍后重试'
  } finally {
    downloading.value = false
  }
}
async function sniffImageMime(blob: Blob) {
  const bytes = new Uint8Array(await blob.slice(0, 12).arrayBuffer())
  if (bytes.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47 && bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a && bytes[7] === 0x0a) return 'image/png'
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg'
  if (bytes.length >= 6 && new TextDecoder().decode(bytes.slice(0, 6)).match(/^GIF8[79]a$/)) return 'image/gif'
  if (bytes.length >= 12 && new TextDecoder().decode(bytes.slice(0, 4)) === 'RIFF' && new TextDecoder().decode(bytes.slice(8, 12)) === 'WEBP') return 'image/webp'
  if (bytes.length >= 2 && bytes[0] === 0x42 && bytes[1] === 0x4d) return 'image/bmp'
  return ''
}
async function load() {
  const version = ++loadVersion; release(); textContent.value = ''; error.value = ''; loading.value = false
  isImage.value = false; isPdf.value = false; isWord.value = false; isSpreadsheet.value = false; isPresentation.value = false; isText.value = false; oversizedPresentation.value = false
  if (!props.active || props.file.contentAccessRequired) return
  const ext = extension(); const mime = String(props.file.mimeType || '').toLowerCase()
  isImage.value = mime.startsWith('image/') || ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(ext)
  isPdf.value = mime === 'application/pdf' || ext === 'pdf'
  isWord.value = ext === 'docx' || mime === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  isSpreadsheet.value = ['xlsx', 'xls', 'csv'].includes(ext) || mime.includes('spreadsheet') || mime.includes('excel')
  isPresentation.value = ['pptx', 'ppt'].includes(ext) || mime.includes('presentation') || mime.includes('powerpoint')
  isText.value = mime.startsWith('text/') || ['txt', 'md', 'csv', 'json', 'xml', 'log'].includes(ext)
  oversizedPresentation.value = isPresentation.value && Number(props.file.fileSizeBytes || 0) > 30 * 1024 * 1024
  if (oversizedPresentation.value) return
  loading.value = true
  try {
    const blob = await getKnowledgeAttachmentContent(props.file.id); if (version !== loadVersion) return
    if (!isImage.value && !isPdf.value && !isWord.value && !isSpreadsheet.value && !isPresentation.value && !isText.value && await sniffImageMime(blob)) isImage.value = true
    // The preview containers are behind the loading branch in the template, so
    // mount them before invoking a renderer that needs a real DOM element.
    loading.value = false
    await nextTick()
    if (version !== loadVersion) return
    if (isImage.value) {
      const detectedMime = await sniffImageMime(blob); blobUrl.value = URL.createObjectURL(detectedMime ? new Blob([blob], { type: detectedMime }) : blob)
    } else if (isPdf.value) {
      pdfDocument = await getDocument({ data: await blob.arrayBuffer() }).promise; if (version !== loadVersion) return
      pdfPages.value = Array.from({ length: pdfDocument.numPages }, (_, index) => index + 1); await nextTick()
      for (const pageNumber of pdfPages.value) {
        if (!pdfDocument || version !== loadVersion) return
        const canvas = pdfCanvases.get(pageNumber); if (!canvas) continue
        const page = await pdfDocument.getPage(pageNumber); const base = page.getViewport({ scale: 1 }); const maxWidth = Math.min(1040, Math.max(320, window.innerWidth - 96)); const viewport = page.getViewport({ scale: Math.min(1.6, maxWidth / base.width) }); const context = canvas.getContext('2d'); if (!context) continue
        canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height); await page.render({ canvasContext: context, canvas, viewport }).promise
      }
    } else if (isWord.value) {
      if (!wordContainer.value) throw new Error('Word preview container unavailable')
      const { renderAsync: renderDocxAsync } = await import('docx-preview')
      await renderDocxAsync(await blob.arrayBuffer(), wordContainer.value, wordContainer.value, {
        className: 'docx', inWrapper: true, ignoreWidth: false, ignoreHeight: false,
        breakPages: true, ignoreLastRenderedPageBreak: false, experimental: true,
      })
    } else if (isSpreadsheet.value) {
      if (!spreadsheetContainer.value) throw new Error('Spreadsheet preview container unavailable')
      const { default: xlsxPreview } = await import('xlsx-preview')
      const rendered = await xlsxPreview.xlsx2Html(blob, { separateSheets: true, minimumRows: 20, minimumCols: 16 })
      const sheets = Array.isArray(rendered) ? await Promise.all(rendered) : [rendered]
      spreadsheetContainer.value.innerHTML = sheets.map((sheet) => typeof sheet === 'string' ? sheet : new TextDecoder().decode(sheet as ArrayBuffer)).join('')
    } else if (isPresentation.value) {
      if (!presentationContainer.value) throw new Error('Presentation preview container unavailable')
      const { init: initPptxPreview } = await import('pptx-preview')
      const previewer = initPptxPreview(presentationContainer.value, { width: 960, height: 540 })
      await previewer.preview(await blob.arrayBuffer())
    } else if (isText.value) textContent.value = await blob.text()
  } catch { if (version === loadVersion) error.value = '附件预览暂不可用' } finally { if (version === loadVersion) loading.value = false }
}
watch(() => [props.active, props.file.id, props.file.type, props.file.name, props.file.mimeType, props.file.contentAccessRequired, props.file.fileSizeBytes], load, { immediate: true })
onUnmounted(() => { loadVersion++; release() })
</script>

<style scoped>
.attachment-preview { min-height: 220px; padding: 16px; border-radius: 8px; background: var(--td-bg-color-secondarycontainer); }
.attachment-preview__state { display: grid; min-height: 180px; place-items: center; gap: 8px; color: var(--td-text-color-secondary); }.attachment-preview__state--error { color: var(--td-error-color); }
.attachment-preview__state--large { align-content:center; text-align:center; }.attachment-preview__state--large svg { width:36px; height:36px; color:var(--td-brand-color); }.attachment-preview__state--large strong { color:var(--td-text-color-primary); }.attachment-preview__state--large span { max-width:52ch; line-height:1.7; }
.attachment-preview__image-box { display: grid; width: 100%; min-height: 220px; max-height: min(72vh, 760px); margin: auto; place-items: center; overflow: auto; background: #fff; border: 1px solid var(--td-border-level-1-color); border-radius: 8px; }.attachment-preview__image-box img { display: block; width: auto; height: auto; max-width: min(100%, 960px); max-height: min(68vh, 680px); object-fit: contain; }
.attachment-preview__pdf { display: grid; gap: 14px; max-height: 74vh; overflow: auto; padding: 4px; }.attachment-preview__pdf-page { display: block; width: min(100%, 1040px); height: auto; margin: 0 auto; background: #fff; box-shadow: 0 1px 5px rgb(0 0 0 / 16%); }
.attachment-preview__source { width: min(100%, 1040px); max-height: 74vh; margin: 0 auto; overflow: auto; padding: 24px; background: #eef0f3; color: var(--td-text-color-primary); box-shadow: 0 1px 5px rgb(0 0 0 / 12%); }
.attachment-preview__word :deep(.docx-wrapper) { padding: 0 !important; background: transparent !important; }.attachment-preview__word :deep(.docx) { margin: 0 auto 18px; }
.attachment-preview__spreadsheet :deep(table) { margin: 0 auto 18px; }
.attachment-preview__presentation { display: flex; justify-content: center; }.attachment-preview__presentation :deep(canvas), .attachment-preview__presentation :deep(svg) { max-width: 100%; height: auto; }
.attachment-preview pre { max-height: 72vh; margin: 0 auto; overflow: auto; color: var(--td-text-color-secondary); font: 13px/1.7 var(--app-font-family); white-space: pre-wrap; }.attachment-preview__unsupported { display: grid; min-height: 260px; place-items: center; align-content: center; gap: 10px; color: var(--td-text-color-secondary); text-align: center; }.attachment-preview__unsupported svg { width: 40px; height: 40px; color: var(--td-brand-color); }.attachment-preview__unsupported strong { color: var(--td-text-color-primary); }.attachment-preview__unsupported span { max-width: 100%; overflow-wrap: anywhere; }
</style>
