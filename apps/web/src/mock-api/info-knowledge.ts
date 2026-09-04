import { useInfoMockStore } from '@/stores/infoMock'
export async function getKnowledgeAttachmentContent(id: string, download = false) { const store = useInfoMockStore(); const file = store.allChats.flatMap((chat) => chat.files).find((item) => String(item.id) === String(id)); return new Blob([file?.content || '本地演示文件内容'], { type: download ? 'application/octet-stream' : 'text/plain' }) }
