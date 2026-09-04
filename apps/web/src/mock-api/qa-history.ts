import { useInfoMockStore } from '@/stores/infoMock'
export type QaAnswerStatus = 'pending' | 'streaming' | 'completed' | 'failed'
export type QaConversation = { id: number | string; title: string; message_count: number; last_message_at?: string | null }
export type QaMessageRecord = { id: number | string; question: string; answer: string; answer_status: QaAnswerStatus; citations?: any[] }
export type QaConversationDetail = QaConversation & { messages: QaMessageRecord[] }
function list(): QaConversation[] { return useInfoMockStore().qaSessions.map((qa, index) => ({ id: qa.id, title: qa.question, message_count: 1, last_message_at: qa.time })) }
export async function listQaConversations() { const items = list(); return { items, page: 1, page_size: 20, total: items.length } }
export async function createQaConversation(title = '新的对话') { return { id: Date.now(), title, message_count: 0 } }
export async function getQaConversation(id: number | string): Promise<QaConversationDetail> { const qa = useInfoMockStore().qaSessions.find((item) => String(item.id) === String(id)); return { id, title: qa?.question || '新的对话', message_count: qa ? 1 : 0, messages: qa ? [{ id: qa.id, question: qa.question, answer: qa.answer, answer_status: 'completed', citations: (qa as any).citations }] : [] } }
export async function renameQaConversation(id: number | string, title: string) { const store = useInfoMockStore(); store.renameQa(String(id), title); return { id, title, message_count: 1 } }
export async function deleteQaConversation(id: number | string) { useInfoMockStore().deleteQa(String(id)); return undefined }
