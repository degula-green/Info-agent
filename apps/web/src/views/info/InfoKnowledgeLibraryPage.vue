<template>
  <section class="library-page">
    <div class="library-page__crumbs">
      <button type="button" @click="router.push('/knowledge')">知识库</button>
      <t-icon name="chevron-right" />
      <span>{{ library?.scope === 'organization' ? '组织知识库' : '私人知识库' }}</span>
      <t-icon name="chevron-right" />
      <strong>{{ library?.name || title }}</strong>
    </div>

    <header class="library-page__header">
      <div>
        <div class="library-page__title-line">
          <span class="library-page__mark" :class="`library-page__mark--${kindKey}`"><t-icon :name="iconName" /></span>
          <div>
            <h1>{{ library?.name || title }}</h1>
            <p>{{ description }}</p>
          </div>
        </div>
      </div>
      <div class="library-page__actions">
        <t-button v-if="isAttachableLibrary" theme="primary" @click="openDiscovery">
          <template #icon><t-icon name="add" /></template>
          {{ isPrivateLibrary ? '接入私聊' : '接入群聊' }}
        </t-button>
        <t-button variant="outline" :loading="loading" @click="loadItems">
          <template #icon><t-icon name="refresh" /></template>
          刷新
        </t-button>
        <label v-if="isFileLibrary" class="upload-button">
          <t-icon name="upload" />
          上传附件
          <input type="file" @change="handleUpload" />
        </label>
      </div>
    </header>

    <div class="library-toolbar">
      <t-input v-model="query" clearable placeholder="搜索本知识库中的消息或文件" @enter="runLibrarySearch" @clear="clearLibrarySearch">
        <template #prefix-icon><t-icon name="search" /></template>
      </t-input>
      <t-select v-model="platform" clearable placeholder="全部平台" class="platform-filter" @change="onPlatformChange">
        <t-option value="feishu" label="飞书" />
        <t-option value="wechat" label="个人微信" />
        <t-option value="wecom" label="企业微信" />
      </t-select>
      <t-button theme="primary" variant="outline" :loading="searchLoading" :disabled="!query.trim()" @click="runLibrarySearch">搜索</t-button>
      <span class="library-toolbar__count">{{ query.trim() ? `${searchResults.length} 条检索` : `${filteredItems.length} 项` }}</span>
    </div>

    <div v-if="error" class="library-alert" role="alert"><t-icon name="error-circle" /><span>{{ error }}</span><t-button size="small" variant="outline" @click="loadItems">重试</t-button></div>
    <div v-if="searchError" class="library-alert" role="alert"><t-icon name="error-circle" /><span>{{ searchError }}</span></div>

    <template v-if="query.trim()">
      <div class="library-search-hint">本知识库检索 · 全文 BM25</div>
      <div v-if="searchLoading" class="library-loading"><t-loading text="正在检索本知识库..." /></div>
      <div v-else-if="searchResults.length" class="library-list">
        <button v-for="item in searchResults" :key="item.id" type="button" class="library-row" @click="selectSearchResult(item)">
          <span class="library-row__icon"><t-icon :name="item.kind === 'file' ? 'file' : item.kind === 'chat' ? 'chat' : 'chat-bubble'" /></span>
          <span class="library-row__main">
            <strong>{{ item.title }}</strong>
            <small>{{ item.subtitle }}</small>
          </span>
          <span class="library-row__meta">
            <span>{{ item.kind === 'file' ? '文件' : item.kind === 'chat' ? '群聊' : '消息' }}</span>
            <span v-if="item.score != null">{{ Number(item.score).toFixed(2) }}</span>
          </span>
          <t-icon name="chevron-right" class="library-row__arrow" />
        </button>
      </div>
      <div v-else class="library-empty">
        <span class="library-empty__icon"><t-icon name="search" /></span>
        <h2>没有匹配内容</h2>
        <p>{{ searchEmptyText }}</p>
      </div>
    </template>

    <template v-else>
    <div v-if="loading" class="library-loading"><t-loading text="正在加载内容..." /></div>
    <div v-else-if="filteredItems.length && isConversationLibrary" class="conversation-grid">
      <button v-for="item in filteredItems" :key="item.id" type="button" class="conversation-card" @click="openItem(item)">
        <span class="conversation-card__head">
          <span class="conversation-card__icon"><t-icon :name="item.conversation_type === 'private' ? 'user' : 'chat'" /></span>
          <span class="conversation-card__type">{{ item.conversation_type === 'private' ? '私聊' : '群聊' }}</span>
          <t-icon name="chevron-right" class="conversation-card__arrow" />
        </span>
        <span class="conversation-card__title">{{ item.title || item.conversation_name || item.external_conversation_id || '未命名会话' }}</span>
        <span class="conversation-card__subtitle">{{ platformName(item.platform) }} · {{ item.conversation_name || item.external_conversation_id || '会话' }}</span>
        <span class="conversation-card__metrics">
          <span><t-icon name="chat-bubble" />{{ item.message_count || 0 }} 条消息</span>
          <span><t-icon name="file" />{{ item.attachment_count || 0 }} 个文件</span>
        </span>
        <span class="conversation-card__status" :class="`conversation-card__status--${collectionStatus(item)}`"><i />{{ collectionStatusLabel(item.collection_status) }}</span>
      </button>
    </div>
    <div v-else-if="filteredItems.length" class="library-list">
      <button v-for="item in filteredItems" :key="item.id" type="button" class="library-row" @click="openItem(item)">
        <span class="library-row__icon"><t-icon :name="item.kind === 'file' ? 'file' : item.kind === 'conversation' ? 'chat' : 'chat-bubble'" /></span>
        <span class="library-row__main">
          <strong>{{ item.title || item.file_name || '未命名条目' }}</strong>
          <small>{{ secondary(item) }}</small>
        </span>
        <span class="library-row__meta">
          <span v-if="item.platform">{{ platformName(item.platform) }}</span>
          <span v-if="item.kind === 'conversation'">{{ item.message_count || 0 }} 条消息 · {{ item.attachment_count || 0 }} 个文件 · {{ collectionStatusLabel(item.collection_status) }}</span>
          <span v-else-if="item.kind === 'file'">{{ formatSize(item.size_bytes) }} · {{ status(item) }}</span>
          <span v-else>{{ status(item) }}</span>
        </span>
        <t-icon name="chevron-right" class="library-row__arrow" />
      </button>
    </div>
    <div v-else class="library-empty">
      <span class="library-empty__icon"><t-icon :name="iconName" /></span>
      <h2>{{ emptyTitle }}</h2>
      <p>{{ emptyDescription }}</p>
      <label v-if="isFileLibrary" class="upload-button upload-button--empty"><t-icon name="upload" />上传第一个附件<input type="file" @change="handleUpload" /></label>
      <t-button v-else-if="isAttachableLibrary" theme="primary" @click="openDiscovery">{{ isPrivateLibrary ? '接入私聊' : '接入群聊' }}</t-button>
    </div>
    </template>

    <t-dialog v-model:visible="discoveryVisible" :header="isPrivateLibrary ? '接入私聊' : '接入群聊'" :footer="false" width="560px">
      <div class="discovery-dialog">
        <p>列表只包含{{ isPrivateLibrary ? '私聊' : '群聊' }}，采集类型由服务端强制校验。</p>
        <t-select v-model="discoveryPlatform" class="discovery-platform" placeholder="选择平台" @change="loadDiscovery">
          <t-option v-for="source in availableConnectors" :key="source.key" :value="source.key" :label="source.name" />
        </t-select>
        <t-button size="small" variant="outline" :loading="discoveryLoading" @click="loadDiscovery"><template #icon><t-icon name="refresh" /></template>刷新列表</t-button>
        <t-input v-model="discoveryQuery" clearable placeholder="搜索名称" class="discovery-dialog__search"><template #prefix-icon><t-icon name="search" /></template></t-input>
        <div v-if="discoveryLoading" class="discovery-dialog__loading"><t-loading text="正在发现会话..." /></div>
        <div v-else-if="discoveryError" class="library-alert"><t-icon name="error-circle" /><span>{{ discoveryError }}</span></div>
        <div v-else class="discovery-list">
          <button v-for="session in discoverySessions" :key="session.external_id" type="button" class="discovery-row" :disabled="Boolean(session.attached_conversation_id)" @click="attach(session)">
            <span class="discovery-row__icon"><t-icon :name="isPrivateLibrary ? 'user' : 'chat'" /></span>
            <span><strong>{{ session.name }}</strong><small>{{ session.member_count }} 位成员 · {{ session.external_id }}</small></span>
            <span class="discovery-row__action">{{ session.attached_conversation_id ? '已接入' : '接入' }}</span>
          </button>
          <p v-if="!discoverySessions.length" class="discovery-empty">没有发现可接入会话。</p>
        </div>
      </div>
    </t-dialog>

    <t-dialog v-model:visible="collectDialogVisible" header="设置采集起点" :confirm-btn="'开始采集'" :cancel-btn="'取消'" @confirm="confirmGroupAttach" @close="resetGroupAttach">
      <div v-if="pendingGroupSession" class="collect-dialog">
        <p>为「{{ pendingGroupSession.name }}」选择采集起点。留空则默认回溯最近 7 天。</p>
        <t-form-item label="采集开始时间">
          <t-input v-model="collectStart" type="datetime-local" :min="historyStartMin" :max="historyStartMax" clearable />
        </t-form-item>
        <div class="collect-dialog__note"><t-icon name="info-circle" />最多回溯 7 天，结束时间默认为当前时间。</div>
      </div>
    </t-dialog>

    <t-dialog v-model:visible="filePreviewVisible" :header="previewFile?.name || '附件预览'" :footer="false" width="min(960px, calc(100vw - 32px))" dialog-class-name="library-file-preview-dialog" placement="center" destroy-on-close>
      <div class="library-file-preview">
        <div v-if="previewSource" class="library-file-preview__source"><t-icon name="chat" /><span>来源：{{ previewSource.name }} · {{ platformName(previewSource.platform) }}</span></div>
        <InfoAttachmentPreview v-if="previewFile" :file="previewFile" :active="filePreviewVisible" />
      </div>
    </t-dialog>

    <t-dialog v-model:visible="shareVisible" header="共享到组织" :footer="false" width="520px">
      <div class="share-dialog">
        <p>仅共享你明确选中的消息或附件，原私人内容仍保持私人权限。</p>
        <div class="share-dialog__summary">已选择 {{ selectedMessageIDs.length + selectedAttachmentIDs.length }} 项</div>
        <div class="share-dialog__actions"><t-button variant="outline" @click="shareVisible = false">取消</t-button><t-button theme="primary" :loading="sharing" :disabled="!selectedMessageIDs.length && !selectedAttachmentIDs.length" @click="shareSelected">共享到组织</t-button></div>
      </div>
    </t-dialog>

    <InfoResultDrawer v-model:visible="drawerVisible" :result="drawerResult" @toast="toast" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { createLocalUploadTask, discoverConversationsByType, getKnowledgeLibraryItems, sharePrivateResources, uploadLocalContent, type AvailableConversationDTO, type KnowledgeLibraryDTO, type KnowledgeLibraryItemDTO } from '@/api/info-knowledge'
import { searchGlobal, searchKnowledge } from '@/api/rag'
import { isHistoryStartAllowed, knowledgeDisplayLabel, mapKnowledgeDisplayStatus } from '@/knowledge-mapping'
import InfoAttachmentPreview from '@/components/InfoAttachmentPreview.vue'
import InfoResultDrawer from '@/components/InfoResultDrawer.vue'
import type { InfoFile, SearchResult } from '@/mock'
import { useInfoKnowledgeStore } from '@/stores/infoKnowledge'
import { resolveLibrarySearchScope, searchEmptyHint } from '@/utils/info-search-scope'
import { isAbortError, mapRagSearchItems } from '@/utils/info-search-result'

const props = defineProps<{ libraryKind: string }>()
const router = useRouter(); const route = useRoute(); const store = useInfoKnowledgeStore()
const library = computed<KnowledgeLibraryDTO | undefined>(() => store.libraries.find((item) => item.base_type === props.libraryKind))
const title = computed(() => ({ organization_files: '文件库', organization_conversation: '群聊', organization_private_shared: '共享私聊', private_conversation: '私聊知识库', private_local: '本地知识库' } as Record<string, string>)[props.libraryKind] || '知识库')
const description = computed(() => ({ organization_files: '组织上传、群聊文件和共享私聊文件的聚合视图', organization_conversation: '组织已接入的群聊消息与采集状态', organization_private_shared: '已明确共享到组织的私聊资源', private_conversation: '当前账号接入的私聊内容', private_local: '当前账号上传的附件' } as Record<string, string>)[props.libraryKind] || '')
const isFileLibrary = computed(() => props.libraryKind === 'organization_files' || props.libraryKind === 'private_local')
const isPrivateLibrary = computed(() => props.libraryKind === 'private_conversation')
const isConversationLibrary = computed(() => props.libraryKind === 'organization_conversation' || props.libraryKind === 'organization_private_shared' || props.libraryKind === 'private_conversation')
const isAttachableLibrary = computed(() => props.libraryKind === 'organization_conversation' || props.libraryKind === 'private_conversation')
const kindKey = computed(() => props.libraryKind.replace('organization_', '').replace('private_', ''))
const iconName = computed(() => isFileLibrary.value ? 'folder-open' : isPrivateLibrary.value || props.libraryKind === 'organization_private_shared' ? 'user-talk' : 'chat')
const items = ref<KnowledgeLibraryItemDTO[]>([]); const loading = ref(false); const error = ref(''); const query = ref(''); const platform = ref('')
const searchResults = ref<SearchResult[]>([])
const searchLoading = ref(false)
const searchError = ref('')
const searchEmptyText = ref('尝试更换关键词，或清除搜索后浏览目录。')
const drawerVisible = ref(false)
const drawerResult = ref<SearchResult | null>(null)
let searchAbort: AbortController | null = null
let searchTimer: ReturnType<typeof setTimeout> | undefined

const filteredItems = computed(() => {
  // Directory view only; RAG results use searchResults when query is set.
  let rows = items.value
  if (platform.value) rows = rows.filter((item) => item.platform === platform.value)
  return rows
})
const emptyTitle = computed(() => isFileLibrary.value ? '还没有附件' : isPrivateLibrary.value ? '还没有接入私聊' : props.libraryKind === 'organization_private_shared' ? '还没有共享私聊' : '还没有接入群聊')
const emptyDescription = computed(() => isFileLibrary.value ? '上传附件后，它会出现在这个知识库中。' : isPrivateLibrary.value ? '接入私聊后，内容默认只归你所有。' : props.libraryKind === 'organization_private_shared' ? '在私人私聊详情中选择消息或附件后，它们会出现在这里。' : '接入群聊后，组织成员可按群聊权限访问内容。')
const discoveryVisible = ref(false); const discoveryLoading = ref(false); const discoveryError = ref(''); const discoveryQuery = ref(''); const discoveryPlatform = ref(''); const discovery = ref<{ discovery_id: string; platform: any; conversations: AvailableConversationDTO[] } | null>(null)
const availableConnectors = computed(() => store.sources.filter((source) => source.bound && source.available !== false))
const discoverySessions = computed(() => (discovery.value?.conversations || []).filter((item) => !discoveryQuery.value.trim() || `${item.name} ${item.external_id}`.toLowerCase().includes(discoveryQuery.value.trim().toLowerCase())))
const collectDialogVisible = ref(false); const pendingGroupSession = ref<AvailableConversationDTO | null>(null); const collectStart = ref('')
function localDateTimeInput(value: Date) {
  const year = value.getFullYear(); const month = String(value.getMonth() + 1).padStart(2, '0'); const day = String(value.getDate()).padStart(2, '0')
  const hours = String(value.getHours()).padStart(2, '0'); const minutes = String(value.getMinutes()).padStart(2, '0')
  return `${year}-${month}-${day}T${hours}:${minutes}`
}
const historyStartMin = localDateTimeInput(new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)); const historyStartMax = localDateTimeInput(new Date())
const shareVisible = ref(false); const sharing = ref(false); const selectedMessageIDs = ref<string[]>([]); const selectedAttachmentIDs = ref<string[]>([])
const filePreviewVisible = ref(false); const previewFile = ref<InfoFile | null>(null); const previewSource = ref<{ name: string; platform?: string } | null>(null)

function platformName(value?: string) { return value === 'feishu' ? '飞书' : value === 'wechat' ? '个人微信' : value === 'wecom' ? '企业微信' : value || '未知平台' }
function formatSize(value?: number) { if (!value) return '-'; if (value < 1024) return `${value} B`; if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1024 / 1024).toFixed(1)} MB` }
function status(item: KnowledgeLibraryItemDTO) {
  return knowledgeDisplayLabel(mapKnowledgeDisplayStatus({
    contentStatus: item.content_status || item.processing_status,
    ragStatus: item.rag_status,
    searchable: item.searchable,
  }))
}
function collectionStatus(item: KnowledgeLibraryItemDTO) {
  const value = String(item.collection_status || '').toLowerCase()
  return value === 'active' ? 'collecting' : value || 'not_started'
}
function collectionStatusLabel(value?: string) {
  switch (String(value || '').toLowerCase()) {
    case 'active':
    case 'collecting': return '采集中'
    case 'paused': return '已暂停'
    case 'detached': return '已解除接入'
    case 'error': return '异常'
    case 'missing': return '已不存在'
    default: return '未开始'
  }
}
function secondary(item: KnowledgeLibraryItemDTO) { if (item.kind === 'conversation') return `${item.conversation_type === 'private' ? '私聊' : '群聊'} · ${item.conversation_name || item.external_conversation_id || ''}`; return item.excerpt || `${item.conversation_name || item.external_conversation_id || ''} · ${item.sent_at ? new Date(item.sent_at).toLocaleString('zh-CN') : ''}` }
function toast(text: string) { MessagePlugin.success(text) }

function openItem(item: KnowledgeLibraryItemDTO) {
  if (item.kind === 'file' && item.source_attachment_id) {
    previewSource.value = item.conversation_name || item.external_conversation_id
      ? { name: item.conversation_name || item.external_conversation_id || '会话', platform: item.platform }
      : null
    previewFile.value = {
      id: item.source_attachment_id,
      name: item.file_name || item.title || '附件',
      type: item.mime_type || item.content_type || 'FILE',
      mimeType: item.mime_type || '',
      size: formatSize(item.size_bytes),
      time: item.sent_at ? new Date(item.sent_at).toLocaleString('zh-CN') : '上传时间未知',
      uploadedAt: item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : '',
      uploader: '',
      content: '',
      timestamp: item.created_at,
      sentAt: item.sent_at ? new Date(item.sent_at).toLocaleString('zh-CN') : '',
      collectedAt: item.updated_at ? new Date(item.updated_at).toLocaleString('zh-CN') : '',
      contentAccessRequired: item.content_access_required,
      documentStatus: item.content_status || item.processing_status || null,
      parseStatus: item.content_status || item.processing_status,
      fileSizeBytes: item.size_bytes,
    }
    filePreviewVisible.value = true
    return
  }
  if (item.conversation_id) {
    void router.push({ path: `/knowledge/${item.platform || 'wechat'}/conversations/${item.conversation_id}`, query: { return: route.fullPath } })
  }
}

function selectSearchResult(result: SearchResult) {
  if (result.kind === 'chat' && result.chatId) {
    const platformKey = result.platform === 'all' ? 'feishu' : result.platform
    void router.push({ path: `/knowledge/${platformKey}/conversations/${result.chatId}`, query: { return: route.fullPath } })
    return
  }
  drawerResult.value = result
  drawerVisible.value = true
}

function clearLibrarySearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchAbort?.abort()
  searchResults.value = []
  searchError.value = ''
  searchEmptyText.value = '尝试更换关键词，或清除搜索后浏览目录。'
  searchLoading.value = false
}

async function runLibrarySearch() {
  const normalized = query.value.trim()
  if (!normalized) {
    clearLibrarySearch()
    return
  }
  if (!library.value?.id) {
    searchError.value = '知识库尚未就绪，请稍后重试'
    searchResults.value = []
    return
  }
  searchAbort?.abort()
  const controller = new AbortController()
  searchAbort = controller
  searchLoading.value = true
  searchError.value = ''
  try {
    // library.id is a logical directory key (e.g. organization:groups:{org}),
    // not an ES knowledge_base_id. Resolve physical UUIDs from attached chats.
    const scope = await resolveLibrarySearchScope(props.libraryKind, items.value)
    if (controller.signal.aborted) return
    const response = scope.mode === 'knowledge' && scope.knowledgeBaseIds.length
      ? await searchKnowledge({
          query: normalized,
          knowledgeBaseIds: scope.knowledgeBaseIds,
          organizationId: scope.organizationId,
          topK: 30,
          signal: controller.signal,
        })
      : await searchGlobal({
          query: normalized,
          organizationId: scope.organizationId,
          knowledgeBaseIds: scope.knowledgeBaseIds,
          topK: 30,
          signal: controller.signal,
        })
    let mapped = mapRagSearchItems(response.items)
    if (platform.value) mapped = mapped.filter((item) => item.platform === platform.value || item.platform === 'all')
    searchResults.value = mapped
    searchEmptyText.value = mapped.length
      ? '尝试更换关键词，或清除搜索后浏览目录。'
      : searchEmptyHint(response.diagnostics)
  } catch (e: any) {
    if (isAbortError(e)) return
    searchResults.value = []
    searchError.value = e?.message || '搜索服务暂不可用'
  } finally {
    if (!controller.signal.aborted) searchLoading.value = false
  }
}

function onPlatformChange() {
  if (query.value.trim()) void runLibrarySearch()
  else void loadItems()
}

watch(query, (value) => {
  if (searchTimer) clearTimeout(searchTimer)
  const normalized = value.trim()
  if (!normalized) {
    clearLibrarySearch()
    return
  }
  searchTimer = setTimeout(() => { void runLibrarySearch() }, 300)
})

async function loadItems() {
  if (!library.value) { await store.ensureLibraries(); if (!library.value) return }
  loading.value = true; error.value = ''
  try { items.value = await getKnowledgeLibraryItems(library.value!.id, { kind: isConversationLibrary.value ? 'conversations' : 'files', platform: platform.value, limit: 200 }) } catch (e: any) { error.value = e?.message || '目录加载失败' } finally { loading.value = false }
}
function openDiscovery() { discoveryVisible.value = true; discoveryQuery.value = ''; void loadDiscovery() }
async function loadDiscovery() {
  discoveryLoading.value = true; discoveryError.value = ''
  try {
    if (!availableConnectors.value.length) await store.ensureSources()
    const connector = availableConnectors.value.find((source) => source.key === discoveryPlatform.value) || availableConnectors.value[0]
    if (!connector) throw new Error('请先绑定可用的平台连接器')
    discoveryPlatform.value = connector.key
    discovery.value = await discoverConversationsByType(connector.key, isPrivateLibrary.value ? 'private' : 'group')
  } catch (e: any) { discoveryError.value = e?.message || '会话发现失败' } finally { discoveryLoading.value = false }
}
async function attach(session: AvailableConversationDTO) {
  if (!discovery.value) return
  if (!isPrivateLibrary.value) {
    pendingGroupSession.value = session
    collectStart.value = ''
    collectDialogVisible.value = true
    return
  }
  await attachSession(session)
}
async function attachSession(session: AvailableConversationDTO, requestedStartAt: string | null = null) {
  if (!discovery.value) return
  try {
    const chat = await store.accessTypedSession(discovery.value.platform, isPrivateLibrary.value ? 'private' : 'group', session, discovery.value.discovery_id, requestedStartAt)
    if (chat) { discoveryVisible.value = false; await loadItems(); MessagePlugin.success(`已接入「${chat.name}」`) }
  } catch (e: any) { MessagePlugin.error(e?.message || '接入失败') }
}
async function confirmGroupAttach() {
  const session = pendingGroupSession.value
  if (!session) return
  if (collectStart.value && !isHistoryStartAllowed(collectStart.value)) {
    MessagePlugin.error('采集起点必须在最近 7 天内，且不能晚于当前时间')
    return
  }
  collectDialogVisible.value = false
  await attachSession(session, collectStart.value ? new Date(collectStart.value).toISOString() : null)
  pendingGroupSession.value = null
  collectStart.value = ''
}
function resetGroupAttach() {
  pendingGroupSession.value = null
  collectStart.value = ''
}
async function handleUpload(event: Event) {
  const input = event.target as HTMLInputElement; const file = input.files?.[0]; if (!file) return
  try {
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer()); const hash = `sha256:${Array.from(new Uint8Array(digest)).map((item) => item.toString(16).padStart(2, '0')).join('')}`
    const requestID = `web-${Date.now()}-${Math.random().toString(16).slice(2)}`
    const task = await createLocalUploadTask({ requestID, traceID: `trace-${requestID}`, uploadDestination: props.libraryKind === 'organization_files' ? 'organization_file_library' : 'private_local_library', fileName: file.name, mimeType: file.type || 'application/octet-stream', sizeBytes: file.size, contentHash: hash })
    await uploadLocalContent(task.request_id, file); await loadItems(); MessagePlugin.success('附件上传完成')
  } catch (e: any) { MessagePlugin.error(e?.message || '附件上传失败') } finally { input.value = '' }
}
function openShare(item: KnowledgeLibraryItemDTO) { selectedMessageIDs.value = item.source_message_id ? [item.source_message_id] : []; selectedAttachmentIDs.value = item.source_attachment_id ? [item.source_attachment_id] : []; shareVisible.value = true }
async function shareSelected() {
  const privateConversationID = String(route.query.conversation || '') || (items.value.find((item) => item.source_message_id === selectedMessageIDs.value[0])?.conversation_id || '')
  if (!privateConversationID) return
  sharing.value = true
  try { await sharePrivateResources({ requestID: `web-share-${Date.now()}`, privateConversationID, messageIDs: selectedMessageIDs.value, attachmentIDs: selectedAttachmentIDs.value }); shareVisible.value = false; MessagePlugin.success('已共享到组织'); await loadItems() } catch (e: any) { MessagePlugin.error(e?.message || '共享失败') } finally { sharing.value = false }
}
onMounted(async () => { await store.ensureSources(); await store.ensureLibraries(); await loadItems() })
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  searchAbort?.abort()
})
</script>

<style scoped lang="less">
.library-page { width: min(1120px, 100%); margin: 0 auto; padding: 30px 38px 60px; }.library-page__crumbs { display: flex; align-items: center; gap: 6px; margin-bottom: 22px; color: var(--td-text-color-secondary); font-size: 12px; }.library-page__crumbs button { padding: 0; border: 0; color: var(--td-brand-color); background: transparent; cursor: pointer; }.library-page__crumbs svg { width: 14px; }.library-page__crumbs strong { color: var(--td-text-color-primary); font-weight: 600; }
.library-page__header { display: flex; align-items: flex-end; justify-content: space-between; gap: 22px; margin-bottom: 29px; }.library-page__title-line { display: flex; align-items: center; gap: 13px; }.library-page__mark { display: inline-grid; flex: 0 0 44px; place-items: center; width: 44px; height: 44px; border-radius: 9px; color: #fff; background: var(--td-brand-color-6); }.library-page__mark--files { background: #2d6cdf; }.library-page__mark--conversation { background: #0b9b7a; }.library-page__mark--private-shared { background: #8d5bd1; }.library-page__mark--private { background: #c45c5c; }.library-page__mark :deep(svg) { width: 22px; height: 22px; }.library-page h1 { margin: 0; font-size: 25px; font-weight: 650; }.library-page__title-line p { margin: 6px 0 0; color: var(--td-text-color-secondary); font-size: 12px; }.library-page__actions { display: flex; align-items: center; gap: 9px; }.upload-button { display: inline-flex; position: relative; align-items: center; gap: 6px; min-height: 32px; padding: 0 13px; border: 1px solid var(--td-brand-color); border-radius: 3px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); font-size: 12px; cursor: pointer; }.upload-button input { position: absolute; width: 1px; height: 1px; overflow: hidden; opacity: 0; pointer-events: none; }
.library-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 15px; }.library-toolbar :deep(.t-input) { max-width: 430px; flex: 1; }.platform-filter { width: 145px; }.library-toolbar__count { margin-left: auto; color: var(--td-text-color-placeholder); font-size: 12px; }.library-search-hint { margin: -6px 0 12px; color: var(--td-text-color-placeholder); font-size: 11px; }.library-alert { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; padding: 10px 12px; border: 1px solid var(--td-error-color-3); border-radius: 6px; color: var(--td-error-color-7); background: var(--td-error-color-1); font-size: 12px; }.library-alert span { flex: 1; min-width: 0; }.library-loading { display: grid; min-height: 230px; place-items: center; }
.collect-dialog p { margin: 0 0 16px; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }.collect-dialog__note { display: flex; align-items: center; gap: 6px; margin-top: 12px; color: var(--td-text-color-placeholder); font-size: 11px; }
.library-list { overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }.library-row { display: flex; align-items: center; width: 100%; min-height: 72px; gap: 12px; padding: 12px 16px; border: 0; border-bottom: 1px solid var(--td-component-stroke); color: var(--td-text-color-primary); background: transparent; text-align: left; cursor: pointer; }.library-row:last-child { border-bottom: 0; }.library-row:hover { background: var(--td-bg-color-container-hover); }.library-row__icon { display: inline-grid; flex: 0 0 32px; place-items: center; width: 32px; height: 32px; border-radius: 7px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); }.library-row__main { display: flex; min-width: 0; flex: 1; flex-direction: column; }.library-row__main strong { overflow: hidden; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }.library-row__main small { margin-top: 5px; overflow: hidden; color: var(--td-text-color-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.library-row__meta { display: flex; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; gap: 5px 12px; color: var(--td-text-color-placeholder); font-size: 11px; }.library-row__arrow { flex: 0 0 15px; width: 15px; color: var(--td-text-color-placeholder); }.library-empty { display: grid; min-height: 280px; place-items: center; align-content: center; gap: 9px; border: 1px dashed var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-secondary); text-align: center; }.library-empty__icon { display: inline-grid; place-items: center; width: 45px; height: 45px; border-radius: 9px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); }.library-empty h2 { margin: 3px 0 0; color: var(--td-text-color-primary); font-size: 16px; }.library-empty p { margin: 0 0 8px; font-size: 12px; }.upload-button--empty { margin-top: 4px; }.discovery-dialog > p { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.6; }.discovery-platform { width: 100%; margin-top: 12px; }.discovery-dialog > .t-button { margin-top: 13px; }.discovery-dialog__search { margin-top: 15px; }.discovery-dialog__loading { display: grid; min-height: 160px; place-items: center; }.discovery-list { display: grid; max-height: 390px; gap: 7px; margin-top: 13px; overflow: auto; }.discovery-row { display: flex; align-items: center; gap: 10px; min-height: 59px; padding: 10px; border: 1px solid var(--td-component-stroke); border-radius: 7px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); text-align: left; cursor: pointer; }.discovery-row:hover { border-color: var(--td-brand-color); background: var(--td-bg-color-container-hover); }.discovery-row:disabled { cursor: default; opacity: .62; }.discovery-row__icon { display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 6px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); }.discovery-row > span:nth-child(2) { display: flex; min-width: 0; flex: 1; flex-direction: column; }.discovery-row strong { overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }.discovery-row small { margin-top: 3px; color: var(--td-text-color-secondary); font-size: 10px; }.discovery-row__action { color: var(--td-brand-color-7); font-size: 11px; }.discovery-empty { padding: 36px; color: var(--td-text-color-placeholder); text-align: center; font-size: 12px; }.share-dialog p { margin: 0; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.7; }.share-dialog__summary { margin-top: 18px; padding: 12px; color: var(--td-text-color-primary); background: var(--td-bg-color-secondarycontainer); font-size: 13px; }.share-dialog__actions { display: flex; justify-content: flex-end; gap: 9px; margin-top: 20px; }
.conversation-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 14px; }
.conversation-card { display: flex; min-width: 0; min-height: 190px; flex-direction: column; gap: 10px; padding: 17px; border: 1px solid var(--td-component-stroke); border-radius: 10px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); text-align: left; cursor: pointer; transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease; }
.conversation-card:hover { border-color: var(--td-brand-color-4); box-shadow: 0 8px 22px rgb(0 0 0 / 7%); transform: translateY(-1px); }
.conversation-card:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
.conversation-card__status { display: inline-flex; align-items: center; gap: 5px; color: var(--td-text-color-placeholder); font-size: 11px; }.conversation-card__status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }.conversation-card__status--collecting { color: var(--td-success-color) !important; }.conversation-card__status--paused { color: var(--td-warning-color) !important; }.conversation-card__status--detached, .conversation-card__status--not_started { color: var(--td-text-color-placeholder) !important; }.conversation-card__status--error, .conversation-card__status--missing { color: var(--td-error-color) !important; }
  .library-file-preview { display: flex; min-width: 0; height: min(calc(100dvh - 150px), 760px); max-height: calc(100dvh - 150px); flex-direction: column; overflow: hidden; }.library-file-preview__source { display: flex; align-items: center; gap: 6px; min-height: 34px; flex: 0 0 34px; padding: 0 16px; color: var(--td-text-color-secondary); font-size: 12px; }.library-file-preview :deep(.attachment-preview) { height: auto; max-height: none; flex: 1; overflow: hidden; }.library-file-preview :deep(.attachment-preview__source), .library-file-preview :deep(.attachment-preview__pdf), .library-file-preview :deep(.attachment-preview pre) { max-height: none; }
:global(.library-file-preview-dialog.t-dialog) { max-height: calc(100dvh - 32px); margin: 0 auto; overflow: hidden; }.library-file-preview-dialog :deep(.t-dialog__body), :global(.library-file-preview-dialog .t-dialog__body) { min-height: 0; max-height: calc(100dvh - 112px); overflow: hidden; }
:global(.t-dialog__wrap:has(.library-file-preview-dialog)) { overflow: hidden; }
:global(.t-dialog__wrap:has(.library-file-preview-dialog) .t-dialog__position) { min-height: 100%; height: 100%; display: flex; align-items: center; justify-content: center; box-sizing: border-box; }
.conversation-card__head { display: flex; align-items: center; gap: 9px; }.conversation-card__icon { display: inline-grid; flex: 0 0 38px; place-items: center; width: 38px; height: 38px; border-radius: 9px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); }.conversation-card__icon :deep(svg) { width: 19px; height: 19px; }.conversation-card__type { color: var(--td-text-color-secondary); font-size: 11px; }.conversation-card__arrow { width: 15px; margin-left: auto; color: var(--td-text-color-placeholder); }.conversation-card__title { min-width: 0; overflow: hidden; font-size: 16px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }.conversation-card__subtitle { min-width: 0; overflow: hidden; color: var(--td-text-color-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.conversation-card__metrics { display: flex; flex-wrap: wrap; gap: 10px 14px; margin-top: auto; color: var(--td-text-color-secondary); font-size: 11px; }.conversation-card__metrics span { display: inline-flex; align-items: center; gap: 4px; }.conversation-card__metrics :deep(svg) { width: 13px; height: 13px; }.conversation-card__status { color: var(--td-brand-color-7); font-size: 11px; }
@media (max-width: 700px) { .library-page { padding: 24px 16px 45px; }.library-page__header { align-items: stretch; flex-direction: column; }.library-page__actions { justify-content: flex-start; flex-wrap: wrap; }.library-toolbar { align-items: stretch; flex-wrap: wrap; }.library-toolbar :deep(.t-input) { max-width: none; flex-basis: 100%; }.platform-filter { width: calc(100% - 64px); }.library-toolbar__count { align-self: center; margin-left: auto; }.library-row { align-items: flex-start; }.library-row__meta { display: none; }.library-row__arrow { margin-top: 9px; }.conversation-grid { grid-template-columns: 1fr; } }
</style>
