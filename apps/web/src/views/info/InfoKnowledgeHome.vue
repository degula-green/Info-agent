<template>
  <section class="knowledge-home">
    <div class="knowledge-home__topline">
      <div>
        <p class="knowledge-home__eyebrow">Knowledge workspace</p>
        <h1>知识库</h1>
        <p class="knowledge-home__intro">按归属查看团队资料与个人采集内容。平台仅作为列表筛选条件。</p>
      </div>
      <t-button variant="outline" :loading="store.loading" @click="refresh">
        <template #icon><t-icon name="refresh" /></template>
        刷新目录
      </t-button>
    </div>

    <div v-if="store.loadError" class="knowledge-alert" role="alert">
      <t-icon name="error-circle" />
      <span>{{ store.loadError }}</span>
      <t-button size="small" variant="outline" @click="refresh">重试</t-button>
    </div>

    <div v-if="store.loading && !store.libraries.length" class="knowledge-loading"><t-loading text="正在加载知识库目录..." /></div>

    <template v-else>
      <section class="library-section">
        <div class="library-section__heading">
          <div>
            <h2>组织知识库</h2>
            <p>团队协作内容、群聊资料与明确共享的私聊内容。</p>
          </div>
          <span>{{ organizationLibraries.length }} 个库</span>
        </div>
        <div v-if="organizationLibraries.length" class="library-grid">
          <button v-for="library in organizationLibraries" :key="library.id" class="library-card" type="button" @click="openLibrary(library)">
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
        <div v-else class="library-empty">当前账号还没有可见的组织知识库。</div>
      </section>

      <section class="library-section">
        <div class="library-section__heading">
          <div>
            <h2>私人知识库</h2>
            <p>只有你可以访问的私聊采集与本地附件。</p>
          </div>
          <span>{{ personalLibraries.length }} 个库</span>
        </div>
        <div class="library-grid">
          <button v-for="library in personalLibraries" :key="library.id" class="library-card" type="button" @click="openLibrary(library)">
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
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import type { KnowledgeLibraryDTO } from '@/api/info-knowledge'
import { useInfoKnowledgeStore } from '@/stores/infoKnowledge'

const router = useRouter()
const store = useInfoKnowledgeStore()
const organizationLibraries = computed(() => store.libraries.filter((library) => library.scope === 'organization'))
const personalLibraries = computed(() => store.libraries.filter((library) => library.scope === 'personal'))

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

async function refresh() {
  try { await store.refreshLibraries() } catch { /* error is displayed in the page */ }
}

onMounted(() => { void store.ensureLibraries() })
</script>

<style scoped lang="less">
.knowledge-home { width: min(1120px, 100%); margin: 0 auto; padding: 36px 38px 64px; }
.knowledge-home__topline { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 38px; }
.knowledge-home__eyebrow { margin: 0 0 8px; color: var(--td-brand-color-7); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 0; font-size: 30px; font-weight: 650; line-height: 1.25; }
.knowledge-home__intro { max-width: 620px; margin: 10px 0 0; color: var(--td-text-color-secondary); font-size: 13px; line-height: 1.65; }
.knowledge-alert { display: flex; align-items: center; gap: 9px; margin-bottom: 20px; padding: 11px 13px; border: 1px solid var(--td-error-color-3); border-radius: 7px; color: var(--td-error-color-7); background: var(--td-error-color-1); font-size: 12px; }
.knowledge-alert span { flex: 1; min-width: 0; }.knowledge-loading { display: grid; min-height: 260px; place-items: center; }
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
@media (max-width: 680px) { .knowledge-home { padding: 26px 16px 48px; }.knowledge-home__topline { align-items: stretch; flex-direction: column; margin-bottom: 28px; }.knowledge-home__topline :deep(.t-button) { align-self: flex-start; }.library-grid { grid-template-columns: 1fr; }.library-section__heading { align-items: flex-start; }.library-section__heading > span { margin-top: 4px; } }
</style>
