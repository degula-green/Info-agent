import { useInfoMockStore } from '@/stores/infoMock'
import { searchMock, type SearchResult, type SourceKey } from '@/mock'
export type SearchPlatform = 'all' | SourceKey
export type InfoSearchRequest = { query: string; platforms?: string[]; page?: number; page_size?: number }
export async function searchInfo(dataOrQuery: InfoSearchRequest | string, platform: SearchPlatform = 'all'): Promise<{ items: SearchResult[]; total: number }> { const data = typeof dataOrQuery === 'string' ? { query: dataOrQuery } : dataOrQuery; await new Promise((resolve) => window.setTimeout(resolve, 180)); const selected = data.platforms?.[0] || platform; const items = searchMock(data.query, selected as SourceKey | 'all', useInfoMockStore().sources, useInfoMockStore().qaSessions); return { items, total: items.length } }
