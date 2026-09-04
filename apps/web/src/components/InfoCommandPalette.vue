<template>
  <t-dialog
    v-model:visible="open"
    :footer="false"
    :header="false"
    :close-btn="false"
    width="680px"
    top="10vh"
    destroy-on-close
    class="cmdk-dialog"
    @opened="focusInput"
  >
    <div class="cmdk" @keydown="onKeyDown">
      <div class="cmdk__input-row">
        <t-icon name="search" class="cmdk__input-icon" />
        <input
          ref="inputRef"
          v-model="localQuery"
          class="cmdk__input"
          placeholder="输入关键词或问题描述，同时触发关键词和向量检索。"
          autocomplete="off"
          spellcheck="false"
          @keydown.stop="onInputKeyDown"
        />
        <button type="button" class="cmdk__close" aria-label="关闭搜索" @click="close">
          <t-icon name="close" size="16px" />
          <span>关闭</span>
        </button>
        <span class="cmdk__key-hint"><kbd>Ctrl</kbd><kbd>K</kbd></span>
      </div>

      <div class="cmdk__results">
        <template v-if="!localQuery.trim()">
          <ResultGroup label="开始搜索">
            <ResultItem
              :index="0"
              icon-name="search"
              title="输入关键词或问题描述开始检索"
              subtitle="自动同时进行关键词和向量检索，支持群聊、消息、文件和历史问答"
              :selected="selectedIndex === 0"
              @primary="focusInput"
              @hover="selectedIndex = $event"
            />
          </ResultGroup>
          <ResultGroup v-if="recentSearches.length" label="最近搜索">
            <ResultItem
              v-for="(item, index) in recentSearches"
              :key="item"
              :index="index + 1"
              icon-name="history"
              :title="item"
              subtitle="再次搜索"
              :selected="selectedIndex === index + 1"
              @primary="useRecentSearch(item)"
              @hover="selectedIndex = $event"
            />
          </ResultGroup>
        </template>

        <template v-else-if="loading">
          <div class="cmdk__empty"><t-icon name="loading" size="32px" /><p>正在搜索…</p><span>正在查询个人微信知识库，请稍候</span></div>
        </template>

        <template v-else-if="results.length">
          <div class="cmdk__tabs" role="tablist" aria-label="搜索结果类型">
            <button
              v-for="tab in tabs"
              :key="tab.key"
              type="button"
              role="tab"
              :aria-selected="activeTab === tab.key"
              :class="['cmdk__tab', { 'cmdk__tab--active': activeTab === tab.key }]"
              @click="activeTab = tab.key; selectedIndex = 0"
            >
              {{ tab.label }}
              <span class="cmdk__tab-count">{{ tab.count }}</span>
            </button>
          </div>

          <div class="cmdk__tab-content">
            <template v-if="activeTab === 'knowledge'">
              <ResultGroup v-if="chatResults.length" label="群聊空间" :count="chatResults.length">
                <ResultItem
                  v-for="(result, index) in chatResults"
                  :key="result.id"
                  :index="index"
                  :icon-name="iconFor(result.kind)"
                  :title="result.title"
                  :subtitle="result.subtitle"
                  badge="群聊"
                  :selected="selectedIndex === index"
                  @primary="select(result)"
                  @hover="selectedIndex = $event"
                />
              </ResultGroup>
              <ResultGroup v-if="messageResults.length" label="消息匹配" :count="messageResults.length">
                <ResultItem
                  v-for="(result, index) in messageResults"
                  :key="result.id"
                  :index="chatResults.length + index"
                  icon-name="chat-bubble"
                  :title="result.title"
                  :subtitle="result.subtitle"
                  badge="消息"
                  :selected="selectedIndex === chatResults.length + index"
                  @primary="select(result)"
                  @hover="selectedIndex = chatResults.length + index"
                />
              </ResultGroup>
              <ResultGroup v-if="fileResults.length" label="文件匹配" :count="fileResults.length">
                <ResultItem
                  v-for="(result, index) in fileResults"
                  :key="result.id"
                  :index="chatResults.length + messageResults.length + index"
                  icon-name="file"
                  :title="result.title"
                  :subtitle="result.subtitle"
                  badge="文件"
                  badge-variant="keyword"
                  :selected="selectedIndex === chatResults.length + messageResults.length + index"
                  @primary="select(result)"
                  @hover="selectedIndex = chatResults.length + messageResults.length + index"
                />
              </ResultGroup>
              <div v-if="!knowledgeResults.length" class="cmdk__tab-empty">知识库中没有匹配内容</div>
            </template>

            <template v-else>
              <ResultGroup label="对话历史" :count="qaResults.length">
                <ResultItem
                  v-for="(result, index) in qaResults"
                  :key="result.id"
                  :index="index"
                  icon-name="chat-bubble-help"
                  :title="result.title"
                  :subtitle="result.subtitle"
                  badge="问答"
                  badge-variant="vector"
                  :selected="selectedIndex === index"
                  @primary="select(result)"
                  @hover="selectedIndex = $event"
                />
              </ResultGroup>
              <div v-if="!qaResults.length" class="cmdk__tab-empty">没有匹配的历史问答</div>
            </template>
          </div>
        </template>

        <div v-else class="cmdk__empty">
          <t-icon name="search" size="32px" />
          <p>没有找到相关内容</p>
          <span>试试更短的关键词，或搜索群聊名称、发送人和文件名</span>
        </div>
      </div>

      <div class="cmdk__footer">
        <span><kbd>↑</kbd><kbd>↓</kbd> 导航</span>
        <span><kbd>Enter</kbd> 打开</span>
        <span><kbd>Tab</kbd> 切换结果</span>
        <span><kbd>Esc</kbd> 关闭</span>
        <span class="cmdk__mode-label">自动混合检索</span>
      </div>
    </div>
  </t-dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import ResultGroup from './GlobalCommandPalette/ResultGroup.vue'
import ResultItem from './GlobalCommandPalette/ResultItem.vue'
import type { SearchResult } from '../mock'

type SearchTab = 'knowledge' | 'history'

const props = defineProps<{ visible: boolean; query: string; results: SearchResult[]; recentSearches?: string[]; loading?: boolean }>()
const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'search', query: string, committed?: boolean): void
  (event: 'select', result: SearchResult): void
}>()

const inputRef = ref<HTMLInputElement>()
const localQuery = ref(props.query)
const activeTab = ref<SearchTab>('knowledge')
const selectedIndex = ref(0)
const open = computed({ get: () => props.visible, set: (value: boolean) => emit('update:visible', value) })
const recentSearches = computed(() => (props.recentSearches || []).slice(0, 5))
const knowledgeResults = computed(() => props.results.filter((result) => result.kind !== 'qa'))
const chatResults = computed(() => props.results.filter((result) => result.kind === 'chat'))
const messageResults = computed(() => props.results.filter((result) => result.kind === 'message'))
const fileResults = computed(() => props.results.filter((result) => result.kind === 'file'))
const qaResults = computed(() => props.results.filter((result) => result.kind === 'qa'))
const tabs = computed(() => [
  { key: 'knowledge' as const, label: '知识库', count: knowledgeResults.value.length },
  { key: 'history' as const, label: '对话历史', count: qaResults.value.length },
])

watch(() => props.query, (value) => { localQuery.value = value })
watch(() => props.results, (results) => {
  selectedIndex.value = 0
  if (!knowledgeResults.value.length && qaResults.value.length) activeTab.value = 'history'
  else if (knowledgeResults.value.length) activeTab.value = 'knowledge'
  else if (!results.length) activeTab.value = 'knowledge'
}, { deep: false })
watch(localQuery, (value) => emit('search', value))

function iconFor(kind: SearchResult['kind']) {
  return kind === 'chat' ? 'folder' : kind === 'file' ? 'file' : 'chat-bubble'
}

function select(result: SearchResult) { emit('select', result) }
function useRecentSearch(query: string) {
  localQuery.value = query
  nextTick(() => inputRef.value?.focus())
}
function submit() {
  const query = localQuery.value.trim()
  if (query) emit('search', query, true)
  else focusInput()
}
function close() { emit('update:visible', false) }
function focusInput() { nextTick(() => inputRef.value?.focus()) }

function flatItems() {
  if (!localQuery.value.trim()) return []
  return activeTab.value === 'history' ? qaResults.value : [...chatResults.value, ...messageResults.value, ...fileResults.value]
}
function onInputKeyDown(event: KeyboardEvent) {
  if (event.key === 'Enter') {
    event.preventDefault()
    if (flatItems()[selectedIndex.value]) select(flatItems()[selectedIndex.value])
    else submit()
  }
  if (event.key === 'Escape') close()
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Tab') {
    event.preventDefault()
    onKeyDown(event)
  }
}
function onKeyDown(event: KeyboardEvent) {
  const items = flatItems()
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!items.length) return
    const direction = event.key === 'ArrowDown' ? 1 : -1
    selectedIndex.value = (selectedIndex.value + direction + items.length) % items.length
    event.preventDefault()
  } else if (event.key === 'Tab' && localQuery.value.trim()) {
    activeTab.value = activeTab.value === 'knowledge' ? 'history' : 'knowledge'
    selectedIndex.value = 0
    event.preventDefault()
  } else if (event.key === 'Escape') close()
}
</script>

<style lang="less" scoped>
.cmdk { display: flex; flex-direction: column; min-height: 340px; max-height: 68vh; color: var(--td-text-color-primary); }
.cmdk__input-row { display: flex; align-items: center; gap: 10px; padding: 14px 16px; border-bottom: 1px solid var(--td-component-stroke); }
.cmdk__input-icon { flex: 0 0 auto; color: var(--td-text-color-placeholder); font-size: 18px; }
.cmdk__input { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--td-text-color-primary); font-size: 15px; }
.cmdk__input::placeholder { color: var(--td-text-color-placeholder); }
.cmdk__close { display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto; padding: 5px 4px; border: 0; background: transparent; color: var(--td-text-color-secondary); font-size: 12px; cursor: pointer; }
.cmdk__close:hover { color: var(--td-text-color-primary); }
.cmdk__key-hint { display: inline-flex; gap: 3px; flex: 0 0 auto; }
.cmdk__key-hint kbd, .cmdk__footer kbd { min-width: 20px; padding: 2px 5px; border: 1px solid var(--td-component-stroke); border-radius: 4px; background: var(--td-bg-color-secondarycontainer); color: var(--td-text-color-secondary); text-align: center; font-size: 10px; line-height: 1.25; }
.cmdk__results { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 6px; }
.cmdk__tabs { display: flex; gap: 8px; margin: -8px -6px 12px; padding: 8px 12px; border-bottom: 1px solid var(--td-component-stroke); background: var(--td-bg-color-secondarycontainer); }
.cmdk__tab { display: inline-flex; align-items: center; gap: 7px; padding: 8px 13px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--td-text-color-secondary); font-size: 13px; cursor: pointer; }
.cmdk__tab:hover { color: var(--td-text-color-primary); }
.cmdk__tab--active { border-color: var(--td-component-stroke); background: var(--td-bg-color-container); color: var(--td-brand-color); box-shadow: 0 1px 3px rgba(0, 0, 0, .05); }
.cmdk__tab-count { display: inline-grid; min-width: 20px; height: 20px; place-items: center; padding: 0 5px; border-radius: 10px; background: var(--td-bg-color-secondarycontainer); color: var(--td-text-color-placeholder); font-size: 11px; }
.cmdk__tab--active .cmdk__tab-count { background: rgba(7, 192, 95, .12); color: var(--td-brand-color); }
.cmdk__tab-content { padding: 0 2px; }
.cmdk__tab-empty { padding: 45px 20px; color: var(--td-text-color-placeholder); text-align: center; font-size: 12px; }
.cmdk__empty { display: flex; flex-direction: column; align-items: center; gap: 9px; padding: 56px 20px; color: var(--td-text-color-placeholder); text-align: center; }
.cmdk__empty p { margin: 2px 0 0; color: var(--td-text-color-secondary); font-size: 13px; }
.cmdk__empty span { font-size: 11px; }
.cmdk__footer { display: flex; align-items: center; flex-wrap: wrap; gap: 15px; padding: 9px 16px; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-placeholder); font-size: 11px; }
.cmdk__footer span { display: inline-flex; align-items: center; gap: 4px; }
.cmdk__mode-label { margin-left: auto; }
@media (max-width: 640px) {
  .cmdk__input-row { gap: 7px; padding: 12px; }
  .cmdk__input { font-size: 14px; }
  .cmdk__close span, .cmdk__key-hint { display: none; }
  .cmdk__footer { gap: 10px; }
  .cmdk__mode-label { width: 100%; margin-left: 0; }
}
</style>

<style lang="less">
.cmdk-dialog .t-dialog__body { padding: 0; }
.cmdk-dialog .t-dialog { overflow: hidden; padding: 0; border-radius: 12px; }
.cmdk-dialog .t-dialog__mask { background: rgba(18, 25, 38, .28); backdrop-filter: blur(7px); }
</style>
