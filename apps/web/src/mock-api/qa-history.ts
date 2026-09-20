// Compatibility exports retained for existing view imports. The implementation
// delegates to the real RAG conversation API.
export { listQaConversations, createQaConversation, getQaConversation, renameQaConversation, deleteQaConversation } from '@/api/rag'
export type { QaConversation, QaMessage as QaMessageRecord } from '@/api/rag'
