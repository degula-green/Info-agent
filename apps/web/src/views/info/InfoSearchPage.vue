<template>
  <section class="search-page">
    <div class="search-page__header"><div><h1>全局搜索</h1><p>搜索三个平台的群聊、消息、文件和历史问答，始终保留来源上下文。</p></div><span class="search-page__hint"><kbd>Ctrl</kbd><kbd>K</kbd> 随时打开搜索</span></div>
    <div class="search-box"><t-icon name="search" /><input ref="inputRef" v-model="query" placeholder="输入关键词或问题描述，同时触发关键词和向量检索。" @keydown.enter="runSearch" /><t-button theme="primary" @click="runSearch">搜索</t-button></div>
    <div class="search-toolbar"><span class="search-mode-hint">自动混合检索</span><t-select v-model="platform" :options="platformOptions" class="platform-filter" @change="runSearch" /></div>
    <div v-if="!query.trim()" class="search-start"><div class="search-start__title"><t-icon name="search" size="20px" /><span>从一个关键词开始</span></div><p>你可以搜索“数据库迁移方案”“版本发布”或“产品讨论组”。结果会按群聊、消息和文件分组。</p><div class="search-recent"><button v-for="item in store.recentSearches" :key="item" @click="query = item; runSearch()"><t-icon name="history" />{{ item }}</button></div></div>
    <template v-else><div class="search-summary"><strong>{{ loading ? '正在搜索…' : total ? `找到 ${total} 条结果` : '没有找到相关内容' }}</strong><span>自动混合检索 · {{ platformLabel }}</span></div><div v-if="error" class="search-empty"><h3>{{ error }}</h3><p>请稍后重试。</p></div><div v-else-if="results.length" class="result-groups"><ResultGroup v-for="group in resultGroups.filter((group) => group.kind !== 'qa')" :key="group.kind" :label="group.label" :count="group.items.length"><ResultItem v-for="(item, index) in group.items" :key="item.id" :index="index" :selected="false" :icon-name="iconFor(item.kind)" :badge="badgeFor(item.kind)" :badge-variant="item.kind === 'file' ? 'keyword' : 'default'" :score="item.score" @primary="selectResult(item)"><template #title><span>{{ item.title }}</span></template><template #subtitle><span>{{ item.subtitle }}</span></template></ResultItem></ResultGroup><div v-if="total > page * pageSize || page > 1" class="search-pagination"><t-button variant="outline" :disabled="page <= 1 || loading" @click="runSearch(page - 1)">上一页</t-button><span>第 {{ page }} 页</span><t-button variant="outline" :disabled="total <= page * pageSize || loading" @click="runSearch(page + 1)">下一页</t-button></div></div><div v-else-if="!loading" class="search-empty"><t-icon name="search" size="34px" /><h3>没有匹配结果</h3><p>请尝试更短的关键词，系统会自动同时进行关键词和向量检索。</p></div></template>
    <InfoResultDrawer v-model:visible="drawerVisible" :result="selectedResult" @toast="toast" @save="saveResult" />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { type SearchResult, type SourceKey } from '@/mock'
import { useInfoMockStore } from '@/stores/infoMock'
import ResultGroup from '@/components/GlobalCommandPalette/ResultGroup.vue'
import ResultItem from '@/components/GlobalCommandPalette/ResultItem.vue'
import InfoResultDrawer from '@/components/InfoResultDrawer.vue'
import { useRouter } from 'vue-router'
import { searchInfo } from '@/mock-api/info-search'
import { mapInfoSearchResult } from '@/utils/info-search-result'
const store = useInfoMockStore(); const router = useRouter(); const query = ref(''); const platform = ref<SourceKey | 'all'>('all'); const results = ref<SearchResult[]>([]); const drawerVisible = ref(false); const selectedResult = ref<SearchResult | null>(null); const inputRef = ref<HTMLInputElement | null>(null); const loading = ref(false); const error = ref(''); const total = ref(0); const page = ref(1); const pageSize = 20
const platformOptions = [{ label: '全部数据', value: 'all' }, { label: '飞书', value: 'feishu' }, { label: '企业微信', value: 'wecom' }, { label: '个人微信', value: 'wechat' }]
const platformLabel = computed(() => platformOptions.find((item) => item.value === platform.value)?.label || '全部数据')
const resultGroups = computed(() => ([['chat', '群聊', results.value.filter((item) => item.kind === 'chat')], ['message', '消息', results.value.filter((item) => item.kind === 'message')], ['file', '文件', results.value.filter((item) => item.kind === 'file')], ['qa', '历史问答', results.value.filter((item) => item.kind === 'qa')]] as const).filter(([, , items]) => items.length).map(([kind, label, items]) => ({ kind, label, items })))
watch(() => query.value, (value) => { if (!value.trim()) results.value = [] })
async function runSearch(nextPage: number | Event = 1) {
  if (!query.value.trim()) return
  const targetPage = typeof nextPage === 'number' ? nextPage : 1
  loading.value = true; error.value = ''; page.value = targetPage
  try {
    const response = await searchInfo({ query: query.value.trim(), platforms: platform.value === 'all' ? [] : [platform.value], page: targetPage, page_size: pageSize }) as any
    const items = Array.isArray(response?.items) ? response.items : []
    results.value = items.filter((item: any) => item.kind !== 'qa').map((item: any) => mapInfoSearchResult(item))
    total.value = Number(response?.total || 0); store.addRecentSearch(query.value.trim())
  } catch (err: any) { results.value = []; total.value = 0; error.value = err?.message || '搜索服务暂不可用' }
  finally { loading.value = false }
}
function iconFor(kind: SearchResult['kind']) { return kind === 'chat' ? 'chat' : kind === 'file' ? 'file' : kind === 'qa' ? 'chat-bubble-help' : 'chat-bubble' }
function badgeFor(kind: SearchResult['kind']) { return kind === 'chat' ? '群聊' : kind === 'file' ? '文件' : kind === 'qa' ? '问答' : '消息' }
function selectResult(result: SearchResult) { if (result.kind === 'chat' && result.chatId) router.push(`/knowledge/${result.platform}/conversations/${result.chatId}`); else { selectedResult.value = result; drawerVisible.value = true } }
function saveResult(result: SearchResult, draft: string) { if (result.kind === 'message' && result.chatId && result.recordId) store.updateMessage(result.chatId, result.recordId, draft); if (result.kind === 'file' && result.chatId && result.recordId) store.updateFile(result.chatId, result.recordId, draft); toast('内容已更新到本地 Mock 数据') }
function toast(text: string) { MessagePlugin.success(text) }
nextTick(() => inputRef.value?.focus())
</script>

<style lang="less" scoped>
.search-page { width: min(1100px, 100%); margin: 0 auto; padding: 32px 34px 56px; }.search-page__header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 25px; }.search-page h1 { margin: 0 0 8px; font-size: 26px; font-weight: 650; }.search-page__header p { margin: 0; color: var(--td-text-color-secondary); font-size: 13px; }.search-page__hint { color: var(--td-text-color-placeholder); font-size: 11px; }.search-page__hint kbd { margin-right: 3px; padding: 2px 5px; border: 1px solid var(--td-component-stroke); border-radius: 3px; }.search-box { display: flex; align-items: center; gap: 11px; padding: 9px 10px 9px 14px; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); box-shadow: 0 2px 10px rgba(0,0,0,.03); }.search-box:focus-within { border-color: var(--td-brand-color); }.search-box > svg { color: var(--td-text-color-placeholder); }.search-box input { flex: 1; min-width: 0; border: 0; outline: 0; color: var(--td-text-color-primary); background: transparent; font-size: 14px; }.search-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 15px; margin: 16px 0 24px; }.search-mode-hint { color: var(--td-text-color-secondary); font-size: 12px; }.platform-filter { width: 150px; }.search-start { padding: 32px 24px; border: 1px dashed var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-page); }.search-start__title { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 600; }.search-start p { max-width: 70ch; margin: 10px 0 18px; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.7; }.search-recent { display: flex; flex-wrap: wrap; gap: 8px; }.search-recent button { display: inline-flex; align-items: center; gap: 5px; padding: 6px 10px; border: 1px solid var(--td-component-stroke); border-radius: 5px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); font-size: 11px; cursor: pointer; }.search-recent button:hover { color: var(--td-brand-color); border-color: var(--td-brand-color); }.search-summary { display: flex; align-items: baseline; gap: 10px; margin-bottom: 9px; }.search-summary strong { font-size: 14px; }.search-summary span { color: var(--td-text-color-placeholder); font-size: 11px; }.result-groups { padding-bottom: 10px; }.search-empty { display: grid; place-items: center; min-height: 270px; border: 1px dashed var(--td-component-stroke); color: var(--td-text-color-placeholder); text-align: center; }.search-empty h3 { margin: 12px 0 4px; color: var(--td-text-color-primary); font-size: 15px; }.search-empty p { margin: 0; font-size: 12px; }
@media (max-width: 720px) { .search-page { padding: 22px 16px 45px; }.search-page__header { align-items: flex-start; flex-direction: column; }.search-toolbar { align-items: stretch; flex-direction: column; }.platform-filter { width: 100%; } }
</style>
