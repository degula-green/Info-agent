import { nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import { getKnowledgeAttachmentContent } from '@/api/info-knowledge'
import type { InfoFile, SearchResult } from '@/mock'
import { navigateToKnowledgeSource } from '@/utils/knowledge-source-navigation'

function previewFileFromResult(result: SearchResult): InfoFile {
  const extension = result.title.includes('.') ? result.title.split('.').pop() || 'file' : 'file'
  return {
    id: String(result.recordId || ''),
    name: result.title,
    type: extension,
    size: '-',
    time: result.time || '发送时间未知',
    uploadedAt: '',
    uploader: result.uploader || '',
    content: result.content || '',
    contentAccessRequired: result.contentAccessRequired,
    documentStatus: 'completed',
    parseStatus: 'completed',
  }
}

export function useSearchResultNavigation() {
  const route = useRoute()
  const router = useRouter()
  const previewVisible = ref(false)
  const previewResult = ref<SearchResult | null>(null)
  const previewFile = ref<InfoFile | null>(null)
  const previewDownloading = ref(false)

  async function openSearchResult(result: SearchResult) {
    const conversationID = result.conversationId || result.chatId
    if (result.kind === 'chat' || result.kind === 'message') {
      if (!conversationID) {
        MessagePlugin.warning(result.kind === 'message' ? '该消息缺少来源会话信息' : '该群聊缺少来源信息')
        return
      }
      try {
        await navigateToKnowledgeSource(router, route, {
          platform: result.platform,
          conversationId: conversationID,
          messageId: result.kind === 'message' ? result.messageId || result.recordId : null,
        })
      } catch {
        MessagePlugin.error('无法定位来源会话，请稍后重试')
      }
      return
    }
    if (result.kind === 'file' && result.recordId) {
      await nextTick()
      previewResult.value = result
      previewFile.value = previewFileFromResult(result)
      previewVisible.value = true
      return
    }
    MessagePlugin.info('该结果暂无可打开的详情')
  }

  async function openPreviewSource() {
    if (!previewResult.value) return
    try {
      await navigateToKnowledgeSource(router, route, {
        platform: previewResult.value.platform,
        conversationId: previewResult.value.conversationId || previewResult.value.chatId,
        attachmentId: previewFile.value?.id,
      })
      previewVisible.value = false
    } catch {
      MessagePlugin.error('无法定位来源会话，请稍后重试')
    }
  }

  async function downloadPreviewFile() {
    if (!previewFile.value || previewFile.value.contentAccessRequired || previewDownloading.value) return
    previewDownloading.value = true
    try {
      const blob = await getKnowledgeAttachmentContent(previewFile.value.id, true)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = previewFile.value.name || 'attachment'
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error: any) {
      MessagePlugin.error(error?.message || '附件下载失败')
    } finally {
      previewDownloading.value = false
    }
  }

  return {
    previewVisible,
    previewResult,
    previewFile,
    previewDownloading,
    openSearchResult,
    openPreviewSource,
    downloadPreviewFile,
  }
}
