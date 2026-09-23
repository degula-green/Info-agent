<template>
  <section class="knowledge-home">
    <div class="knowledge-home__topline">
      <div>
        <p class="knowledge-home__eyebrow">Knowledge workspace</p>
        <h1>知识库</h1>
        <p class="knowledge-home__intro">按归属查看团队资料与个人采集内容。也可在此跨库检索消息与附件。</p>
      </div>
      <t-button variant="outline" :loading="store.loading" @click="refresh">
        <template #icon><t-icon name="refresh" /></template>
        刷新目录
      </t-button>
    </div>

    <div class="knowledge-home__search">
      <t-input v-model="query" clearable placeholder="搜索可见知识库中的消息和附件…" @clear="clearSearch">
        <template #prefix-icon><t-icon name="search" /></template>
      </t-input>
      <span class="knowledge-home__search-hint">全库检索</span>
    </div>

    <div v-if="store.loadError" class="knowledge-alert" role="alert">
      <t-icon name="error-circle" />
      <span>{{ store.loadError }}</span>
      <t-button size="small" variant="outline" @click="refresh">重试</t-button>
    </div>

    <div v-if="query.trim()" class="knowledge-search-panel">
      <div class="knowledge-search-panel__summary">
        <strong>{{ searchLoading ? '正在搜索…' : searchResults.length ? `找到 ${searchResults.length} 条结果` : '没有匹配内容' }}</strong>
        <span>全库检索 · BM25 + 向量</span>
      </div>
      <div v-if="searchError" class="knowledge-alert" role="alert"><t-icon name="error-circle" /><span>{{ searchError }}</span></div>
      <div v-else-if="searchLoading" class="knowledge-loading"><t-loading text="正在检索…" /></div>
      <div v-else-if="searchResults.length" class="knowledge-search-list">
        <button
          v-for="item in searchResults"
          :key="item.id"
          type="button"
          class="knowledge-search-row"
          @click="selectSearchResult(item)"
        >
          <span class="knowledge-search-row__icon"><t-icon :name="item.kind === 'file' ? 'file' : item.kind === 'chat' ? 'chat' : 'chat-bubble'" /></span>
          <span class="knowledge-search-row__main">
            <strong>{{ item.title }}</strong>
            <small>{{ item.subtitle }}</small>
          </span>
          <span class="knowledge-search-row__badge">{{ item.kind === 'file' ? '文件' : item.kind === 'chat' ? '群聊' : '消息' }}</span>
        </button>
      </div>
      <div v-else class="library-empty">没有匹配的消息或附件，可尝试更短的关键词。</div>
    </div>

    <div v-if="store.loading && !store.libraries.length" class="knowledge-loading"><t-loading text="正在加载知识库目录..." /></div>

    <template v-else>
      <section class="library-section">
        <div class="library-section__heading">
          <div>
            <h2>组织知识库</h2>
            <p>团队协作内容、群聊资料与明确共享的私聊内容。</p>
          </div>
          <span>{{ filteredOrganizationLibraries.length }} 个库</span>
        </div>
        <div v-if="filteredOrganizationLibraries.length" class="library-grid">
          <button v-for="library in filteredOrganizationLibraries" :key="library.id" class="library-card" type="button" @click="openLibrary(library)">
            <span class="library-card__icon" :class="`library-card__icon--${iconKey(library)}`"><t-icon :name="iconName(library)" /></span>
            <span class="library-card__body">
              <strong>{{ library.name }}</strong>
              <small>{{ description(library) }}</small>
              <span class="library-card__metrics">
                <span>{{ metricLabel(library) }}</span>
                <span v-if="library.file_count">{{ library.file_count }} 个文件</span>
              </span>
            </span>
            <t-icon name="chevron-right" class="library-card__arrow" />
          </button>
        </div>
        <div v-else class="library-empty">{{ query.trim() ? '没有匹配的组织知识库名称。' : '当前账号还没有可见的组织知识库。' }}</div>
      </section>

      <section class="library-section">
        <div class="library-section__heading">
          <div>
            <h2>私人知识库</h2>
            <p>只有你可以访问的私聊采集与本地附件。</p>
          </div>
          <span>{{ filteredPersonalLibraries.length }} 个库</span>
        </div>
        <div v-if="filteredPersonalLibraries.length" class="library-grid">
          <button v-for="library in filteredPersonalLibraries" :key="library.id" class="library-card" type="button" @click="openLibrary(library)">
            <span class="library-card__icon" :class="`library-card__icon--${iconKey(library)}`"><t-icon :name="iconName(library)" /></span>
            <span class="library-card__body">
              <strong>{{ library.name }}</strong>
              <small>{{ description(library) }}</small>
              <span class="library-card__metrics">
                <span>{{ metricLabel(library) }}</span>
                <span v-if="library.file_count">{{ library.file_count }} 个文件</span>
              </span>
            </span>
            <t-icon name="chevron-right" class="library-card__arrow" />
          </button>
        </div>
        <div v-else class="library-empty">{{ query.trim() ? '没有匹配的私人知识库名称。' : '当前账号还没有私人知识库。' }}</div>
      </section>
    </template>

    <InfoResultDrawer v-model:visible="drawerVisible" :result="drawerResult" @toast="toast" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRouter } from 'vue-router'
import type { KnowledgeLibraryDTO } from '@/api/info-knowledge'
import { searchGlobal } from '@/api/rag'
import InfoResultDrawer from '@/components/InfoResultDrawer.vue'
import type { SearchResult } from '@/mock'
import { useInfoKnowledgeStore } from '@/stores/infoKnowledge'
import { resolveGlobalSearchScope } from '@/utils/info-search-scope'
import { isAbortError, mapRagSearchItems } from '@/utils/info-search-result'

const router = useRouter()
const store = useInfoKnowledgeStore()
const query = ref('')
const searchResults = ref<SearchResult[]>([])
const searchLoading = ref(false)
const searchError = ref('')
const drawerVisible = ref(false)
const drawerResult = ref<SearchResult | null>(null)
let searchTimer: ReturnType<typeof setTimeout> | undefined
let searchAbort: AbortController | null = null
let searchSeq = 0

const organizationLibraries = computed(() => store.libraries.filter((library) => library.scope === 'organization'))
const personalLibraries = computed(() => store.libraries.filter((library) => library.scope === 'personal'))

function matchLibrary(library: KnowledgeLibraryDTO, needle: string) {
  if (!needle) return true
  return `${library.name} ${description(library)} ${library.base_type}`.toLowerCase().includes(needle)
}

const filteredOrganizationLibraries = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return organizationLibraries.value.filter((library) => matchLibrary(library, needle))
})
const filteredPersonalLibraries = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return personalLibraries.value.filter((library) => matchLibrary(library, needle))
})

function description(library: KnowledgeLibraryDTO) {
  if (library.base_type === 'organization_files') return '组织上传、群聊文件和共享私聊文件'
  if (library.base_type === 'organization_conversation') return '已接入组织的群聊消息'
  if (library.base_type === 'organization_private_shared') return '已明确共享到组织的私聊内容'
  if (library.base_type === 'private_conversation') return '当前账号接入的私人聊天'
  return '当前账号上传的本地附件'
}

function metricLabel(library: KnowledgeLibraryDTO) {
  if (library.base_type === 'organization_files' || library.base_type === 'private_local') return `${library.file_count} 个文件`
  return `${library.conversation_count} 个会话`
}

function iconKey(library: KnowledgeLibraryDTO) {
  return library.base_type.replace('organization_', '').replace('private_', '')
}

function iconName(library: KnowledgeLibraryDTO) {
  if (library.base_type === 'organization_files' || library.base_type === 'private_local') return 'folder-open'
  if (library.base_type.includes('private')) return 'user-talk'
  return 'chat'
}

function openLibrary(library: KnowledgeLibraryDTO) {
  const routes: Record<string, string> = {
    organization_files: '/knowledge/organization/files',
    organization_conversation: '/knowledge/organization/groups',
    organization_private_shared: '/knowledge/organization/private-shared',
    private_conversation: '/knowledge/personal/private',
    private_local: '/knowledge/personal/files',
  }
  const path = routes[library.base_type]
  if (path) router.push(path)
}

function clearSearch() {
  query.value = ''
  searchAbort?.abort()
  searchResults.value = []
  searchError.value = ''
  searchLoading.value = false
}

watch(query, (value) => {
  if (searchTimer) clearTimeout(searchTimer)
  const normalized = value.trim()
  if (!normalized) {
    clearSearch()
    return
  }
  const seq = ++searchSeq
  searchLoading.value = true
  searchError.value = ''
  searchTimer = setTimeout(async () => {
    searchAbort?.abort()
    const controller = new AbortController()
    searchAbort = controller
    try {
      const scope = await resolveGlobalSearchScope()
      if (seq !== searchSeq) return
      const response = await searchGlobal({
        query: normalized,
        organizationId: scope.organizationId,
        knowledgeBaseIds: scope.knowledgeBaseIds,
        topK: 20,
        signal: controller.signal,
      })
      if (seq !== searchSeq) return
      searchResults.value = mapRagSearchItems(response.items)
      searchLoading.value = false
    } catch (error) {
      if (isAbortError(error) || seq !== searchSeq) return
      searchResults.value = []
      searchError.value = (error as Error)?.message || '搜索服务暂不可用'
      searchLoading.value = false
    }
  }, 250)
})

function selectSearchResult(result: SearchResult) {
  if (result.kind === 'chat' && result.chatId) {
    const platformKey = result.platform === 'all' ? 'feishu' : result.platform
    router.push(`/knowledge/${platformKey}/conversations/${result.chatId}`)
    return
  }
  drawerResult.value = result
  drawerVisible.value = true
}

function toast(text: string) { MessagePlugin.success(text) }

async function refresh() {
  try { await store.refreshLibraries() } catch { /* error is displayed in the page */ }
}

onMounted(() => { void store.ensureLibraries() })
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  searchAbort?.abort()
})
</script>

<style scoped lang="less">
.knowledge-home { width: min(1120px, 100%); margin: 0 auto; padding: 36px 38px 64px; }
.knowledge-home__topline { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 22px; }
.knowledge-home__eyebrow { margin: 0 0 8px; color: var(--td-brand-color-7); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 0; font-size: 30px; font-weight: 650; line-height: 1.25; }
.knowledge-home__intro { max-width: 620px; margin: 10px 0 0; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }
.knowledge-home__search { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; }
.knowledge-home__search :deep(.t-input) { width: min(520px, 100%); border-radius: 10px; }
.knowledge-home__search-hint { color: var(--td-text-color-placeholder); font-size: 12px; white-space: nowrap; }
.knowledge-alert { display: flex; align-items: center; gap: 9px; margin-bottom: 20px; padding: 11px 13px; border: 1px solid var(--td-error-color-3); border-radius: 7px; color: var(--td-error-color-7); background: var(--td-error-color-1); font-size: 12px; }
.knowledge-alert span { flex: 1; min-width: 0; }.knowledge-loading { display: grid; min-height: 160px; place-items: center; }
.knowledge-search-panel { margin-bottom: 34px; }
.knowledge-search-panel__summary { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.knowledge-search-panel__summary strong { font-size: 14px; }
.knowledge-search-panel__summary span { color: var(--td-text-color-placeholder); font-size: 11px; }
.knowledge-search-list { overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.knowledge-search-row { display: flex; align-items: center; gap: 12px; width: 100%; min-height: 64px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--td-component-stroke); color: var(--td-text-color-primary); background: transparent; text-align: left; cursor: pointer; }
.knowledge-search-row:last-child { border-bottom: 0; }
.knowledge-search-row:hover { background: var(--td-bg-color-container-hover); }
.knowledge-search-row__icon { display: inline-grid; flex: 0 0 32px; place-items: center; width: 32px; height: 32px; border-radius: 7px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); }
.knowledge-search-row__main { display: flex; min-width: 0; flex: 1; flex-direction: column; }
.knowledge-search-row__main strong { overflow: hidden; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.knowledge-search-row__main small { margin-top: 4px; overflow: hidden; color: var(--td-text-color-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.knowledge-search-row__badge { flex: 0 0 auto; color: var(--td-text-color-placeholder); font-size: 11px; }
.library-section { margin-top: 32px; }.library-section + .library-section { margin-top: 46px; }
.library-section__heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 17px; }
.library-section__heading h2 { margin: 0; font-size: 19px; font-weight: 650; }.library-section__heading p { margin: 6px 0 0; color: var(--td-text-color-secondary); font-size: 12px; }.library-section__heading > span { color: var(--td-text-color-placeholder); font-size: 12px; white-space: nowrap; }
.library-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 13px; }
.library-card { display: flex; align-items: flex-start; min-width: 0; min-height: 142px; gap: 13px; padding: 18px 15px; border: 1px solid var(--td-component-stroke); border-radius: 9px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); text-align: left; cursor: pointer; transition: border-color .18s ease, background .18s ease, transform .18s ease; }
.library-card:hover { border-color: var(--td-brand-color-4); background: var(--td-bg-color-container-hover); transform: translateY(-1px); }.library-card:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
.library-card__icon { display: inline-grid; flex: 0 0 38px; place-items: center; width: 38px; height: 38px; border-radius: 8px; color: #fff; background: var(--td-brand-color-6); }.library-card__icon :deep(svg) { width: 20px; height: 20px; }.library-card__icon--files { background: #2d6cdf; }.library-card__icon--conversation { background: #0b9b7a; }.library-card__icon--private-shared { background: #8d5bd1; }.library-card__icon--private { background: #c45c5c; }
.library-card__body { display: flex; min-width: 0; flex: 1; flex-direction: column; }.library-card__body strong { overflow: hidden; font-size: 15px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }.library-card__body small { min-height: 38px; margin-top: 7px; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.55; }.library-card__metrics { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: auto; color: var(--td-text-color-placeholder); font-size: 11px; }.library-card__arrow { flex: 0 0 16px; width: 16px; margin-top: 10px; color: var(--td-text-color-placeholder); }
.library-empty { padding: 28px 16px; border: 1px dashed var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-placeholder); text-align: center; font-size: 12px; }
@media (max-width: 940px) { .library-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 680px) { .knowledge-home { padding: 26px 16px 48px; }.knowledge-home__topline { align-items: stretch; flex-direction: column; margin-bottom: 18px; }.knowledge-home__topline :deep(.t-button) { align-self: flex-start; }.knowledge-home__search { align-items: stretch; flex-direction: column; }.knowledge-home__search :deep(.t-input) { width: 100%; }.library-grid { grid-template-columns: 1fr; }.library-section__heading { align-items: flex-start; }.library-section__heading > span { margin-top: 4px; } }
</style>
