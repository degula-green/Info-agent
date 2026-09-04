<template>
  <aside ref="sidebarRef" class="info-sidebar" :class="{ 'info-sidebar--collapsed': collapsed }">
    <div class="info-sidebar__header">
      <button v-if="!collapsed" class="info-brand" type="button" aria-label="开始新对话" @click="emit('navigate', 'new-chat')">
        <svg class="info-brand__logo" viewBox="0 0 36 36" aria-hidden="true">
          <path d="M4.5 20.4c5.7-.7 10-4.1 12.7-10.3 1.2 5.2.1 9.8-3.4 12.9-2.8 2.4-6.3 3.4-10.3 2.9.1-1.9.4-3.7 1-5.5Z" fill="#08c46a" />
          <path d="M10.4 27.4c7.8-1.3 13.6-6.3 17-14.8 1.5 7.8-.9 13.6-7.4 16.5-4.1 1.8-8.2 1.8-12.2.1.9-.5 1.7-1.1 2.6-1.8Z" fill="#0aaa59" />
          <path d="M3.3 27.2c7.2 4.2 17.3 4.4 26.4-.9-2.3 5.4-8.6 8-15.3 7.1-4.5-.6-8.5-2.8-11.1-6.2Z" fill="#20d678" />
        </svg>
        <span v-if="!collapsed">知识助理</span>
      </button>

      <button
        v-if="!collapsed"
        class="sidebar-toggle"
        type="button"
        aria-label="收起侧边栏"
        title="收起侧边栏"
        @click="collapsed = true"
      >
        <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="1.5" y="1.5" width="17" height="17" rx="3" /><line x1="7.5" y1="1.5" x2="7.5" y2="18.5" /><line x1="4" y1="7.5" x2="4" y2="12.5" /></svg>
      </button>
      <button
        v-else
        class="sidebar-toggle sidebar-toggle--expand"
        type="button"
        aria-label="展开侧边栏"
        title="展开侧边栏"
        @click="collapsed = false"
      >
        <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="1.5" y="1.5" width="17" height="17" rx="3" /><line x1="7.5" y1="1.5" x2="7.5" y2="18.5" /><line x1="5" y1="10" x2="3" y2="8" /><line x1="5" y1="10" x2="3" y2="12" /></svg>
      </button>
    </div>

    <div class="info-sidebar__body">
      <nav class="sidebar-nav" aria-label="主导航">
        <button
          v-for="item in navItems"
          :key="item.key"
          class="sidebar-nav__item"
          :class="{ active: active === item.key }"
          :title="collapsed ? item.label : undefined"
          :aria-current="active === item.key ? 'page' : undefined"
          type="button"
          @click="emit('navigate', item.key)"
        >
          <svg v-if="item.key === 'new-chat' && active === item.key" class="sidebar-nav__icon sidebar-nav__icon--chat-active" viewBox="0 0 20 20" aria-hidden="true">
            <path d="M3.1 3.4h13.8c1 0 1.8.8 1.8 1.8v7.1c0 1-.8 1.8-1.8 1.8H9.5l-2.8 2.4c-.4.4-1.1.1-1.1-.5v-1.9H3.1c-1 0-1.8-.8-1.8-1.8V5.2c0-1 .8-1.8 1.8-1.8Z" fill="currentColor" />
            <path d="M6 7.6h8M6 10.6h5.1" stroke="#fff" stroke-width="1.15" stroke-linecap="round" />
          </svg>
          <svg v-else-if="item.key === 'new-chat'" class="sidebar-nav__icon sidebar-nav__icon--chat" viewBox="0 0 20 20" aria-hidden="true">
            <path d="M3.1 3.4h13.8c1 0 1.8.8 1.8 1.8v7.1c0 1-.8 1.8-1.8 1.8H9.5l-2.8 2.4c-.4.4-1.1.1-1.1-.5v-1.9H3.1c-1 0-1.8-.8-1.8-1.8V5.2c0-1 .8-1.8 1.8-1.8Z" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round" />
            <path d="M6 7.6h8M6 10.6h5.1" stroke="currentColor" stroke-width="1.05" stroke-linecap="round" />
          </svg>
          <t-icon v-else :name="item.icon" />
          <span v-if="!collapsed">{{ item.label }}</span>
          <kbd v-if="!collapsed && item.key === 'search'" class="sidebar-nav__shortcut">Ctrl K</kbd>
        </button>
      </nav>

      <section v-if="!collapsed" class="sidebar-history" aria-label="智能问答历史对话">
        <button
          class="sidebar-history__heading"
          type="button"
          :aria-expanded="!historyCollapsed"
          @click="historyCollapsed = !historyCollapsed"
        >
          <span>历史对话</span>
          <t-icon :name="historyCollapsed ? 'chevron-right' : 'chevron-down'" />
        </button>

        <div v-if="!historyCollapsed" class="sidebar-history__list">
          <button
            v-for="session in qaSessions"
            :key="session.id"
            class="sidebar-history__item"
            type="button"
            :title="session.question"
            @click="emit('qa', session.id)"
          >
            <t-icon name="chat" />
            <span>{{ session.question }}</span>
            <span class="sidebar-history__actions"><button type="button" title="重命名" @click.stop="emit('qa-rename', session.id)"><t-icon name="edit-1" /></button><button type="button" title="删除" @click.stop="emit('qa-delete', session.id)"><t-icon name="delete" /></button></span>
          </button>
          <p v-if="!qaSessions.length" class="sidebar-history__empty">暂无历史对话</p>
        </div>
      </section>
    </div>

    <div class="info-sidebar__footer">
      <button
        class="sidebar-user"
        type="button"
        :title="collapsed ? nickname : undefined"
        :aria-expanded="userMenuOpen"
        aria-haspopup="menu"
        @click.stop="userMenuOpen = !userMenuOpen"
      >
        <span class="sidebar-user__avatar">
          <img v-if="avatarUrl" :src="avatarUrl" alt="" />
          <span v-else>{{ avatarLabel }}</span>
        </span>
        <span v-if="!collapsed" class="sidebar-user__name">{{ nickname }}</span>
        <t-icon v-if="!collapsed" :name="userMenuOpen ? 'chevron-up' : 'chevron-down'" class="sidebar-user__chevron" />
      </button>
      <div v-if="userMenuOpen" class="sidebar-user-menu" role="menu" aria-label="用户菜单" @click.stop>
        <button v-for="item in userMenuItems" :key="item.key" type="button" class="sidebar-user-menu__item" :class="{ 'sidebar-user-menu__item--danger': item.key === 'logout' }" role="menuitem" @click="selectUserMenu(item.key)">
          <t-icon :name="item.icon" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </button>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { QASession } from '../mock'

const props = defineProps<{
  active: string
  nickname: string
  avatar?: string
  avatarUrl?: string | null
  qaSessions: QASession[]
}>()

const emit = defineEmits<{
  (event: 'navigate', view: string): void
  (event: 'qa', id: string): void
  (event: 'qa-rename', id: string): void
  (event: 'qa-delete', id: string): void
  (event: 'collapsed-change', value: boolean): void
  (event: 'menu-action', key: 'profile' | 'terms' | 'privacy' | 'logout'): void
}>()

const collapsed = ref(false)
const historyCollapsed = ref(false)
const userMenuOpen = ref(false)
const sidebarRef = ref<HTMLElement | null>(null)
const avatarLabel = computed(() => props.avatar?.trim() || props.nickname.slice(0, 1).toUpperCase() || '?')
watch(collapsed, (value) => emit('collapsed-change', value))
watch(collapsed, (value) => { if (value) userMenuOpen.value = false })
const userMenuItems = [
  { key: 'profile', label: '个人信息', icon: 'user' },
  { key: 'terms', label: '用户协议', icon: 'file-copy' },
  { key: 'privacy', label: '隐私协议', icon: 'lock-on' },
  { key: 'logout', label: '退出登录', icon: 'logout' },
] as const
function selectUserMenu(key: (typeof userMenuItems)[number]['key']) {
  userMenuOpen.value = false
  emit('menu-action', key)
}
function closeUserMenu(event: PointerEvent) {
  if (userMenuOpen.value && !sidebarRef.value?.contains(event.target as Node)) userMenuOpen.value = false
}
onMounted(() => document.addEventListener('pointerdown', closeUserMenu))
onUnmounted(() => document.removeEventListener('pointerdown', closeUserMenu))
const navItems = [
  { key: 'new-chat', label: '新对话', icon: 'chat-add' },
  { key: 'knowledge', label: '知识库', icon: 'book-open' },
  { key: 'search', label: '搜索', icon: 'search' },
] as const
</script>

<style lang="less" scoped>
.info-sidebar {
  display: flex;
  position: relative;
  flex: 0 0 260px;
  flex-direction: column;
  width: 260px;
  height: 100vh;
  padding: 8px 6px 6px;
  overflow: visible;
  border-right: 1px solid var(--td-component-stroke, #e7e7e7);
  background: var(--td-bg-color-sidebar, #f5f6f7);
  color: var(--td-text-color-primary);
}

.info-sidebar--collapsed {
  flex-basis: 68px;
  width: 68px;
  padding: 0 0 12px;
  border-right-color: transparent;
}

.info-sidebar--collapsed .info-sidebar__header {
  justify-content: center;
  height: 82px;
  min-height: 82px;
  padding: 0;
}

.info-sidebar--collapsed .info-brand {
  display: none;
}

.info-sidebar__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 50px;
  min-height: 50px;
  padding: 0 10px 0 14px;
}

.info-brand,
.sidebar-toggle,
.sidebar-nav__item,
.sidebar-history__heading,
.sidebar-history__item,
.sidebar-user {
  border: 0;
  background: transparent;
  font: inherit;
  cursor: pointer;
}

.info-brand {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  flex: 1;
  gap: 2px;
  padding: 3px 0;
  border-radius: 6px;
  color: #53617a;
  font-size: 17px;
  font-weight: 500;
}

.info-brand:hover {
  background: var(--td-bg-color-container-hover);
}

.info-brand__logo {
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
}

.info-brand span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-toggle {
  display: inline-grid;
  place-items: center;
  flex: 0 0 22px;
  width: 22px;
  height: 22px;
  padding: 0;
  border-radius: 4px;
  color: var(--td-text-color-secondary);
}

.sidebar-toggle:hover {
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-container-hover);
}

.sidebar-toggle svg {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.2;
  stroke-linecap: round;
}

.sidebar-toggle--expand {
  margin-left: auto;
}

.info-sidebar--collapsed .sidebar-toggle--expand {
  margin-left: 0;
}

.info-sidebar__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.sidebar-nav,
.sidebar-history__list {
  display: grid;
  gap: 3px;
}

.sidebar-nav {
  margin-top: 7px;
}

.info-sidebar--collapsed .sidebar-nav {
  gap: 14px;
  margin-top: 6px;
}

.sidebar-nav__item {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 40px;
  gap: 8px;
  padding: 8px 10px 8px 14px;
  margin-bottom: 0;
  border-radius: 4px;
  color: var(--td-text-color-primary);
  text-align: left;
  font-size: 14px;
  font-weight: 500;
}

.sidebar-nav__item:hover {
  background: var(--td-bg-color-container-hover);
}

.sidebar-nav__item.active {
  color: var(--td-brand-color);
  background: transparent;
}

.sidebar-nav__item svg {
  flex: 0 0 20px;
  width: 20px;
  height: 20px;
}

.info-sidebar--collapsed .sidebar-nav__item {
  justify-content: center;
  min-height: 40px;
  padding: 0;
  border-radius: 10px;
}

.info-sidebar--collapsed .sidebar-nav__item svg {
  width: 22px;
  height: 22px;
}

.sidebar-nav__shortcut {
  margin-left: auto;
  padding: 1px 4px;
  border: 0;
  border-radius: 4px;
  color: #e3e5e9;
  font-family: inherit;
  font-size: 11px;
  font-weight: 400;
  line-height: 18px;
}

.sidebar-nav__icon--chat-active {
  color: var(--td-brand-color);
}

.sidebar-history {
  margin-top: 12px;
}

.sidebar-history__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 34px;
  padding: 0 10px 0 14px;
  color: #7f8798;
  text-align: left;
  font-size: 14px;
  font-weight: 500;
}

.sidebar-history__heading:hover {
  color: var(--td-text-color-secondary);
}

.sidebar-history__heading svg {
  width: 16px;
  height: 16px;
}

.sidebar-history__list {
  margin-top: 5px;
}

.sidebar-history__item {
  display: flex;
  align-items: center;
  min-width: 0;
  min-height: 34px;
  gap: 8px;
  padding: 0 10px 0 14px;
  border-radius: 4px;
  color: var(--td-text-color-secondary);
  text-align: left;
  font-size: 13px;
}

.sidebar-history__item:hover {
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-container-hover);
}

.sidebar-history__item svg {
  flex: 0 0 15px;
  width: 15px;
  height: 15px;
}

.sidebar-history__item span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-history__empty {
  margin: 4px 10px;
  color: var(--td-text-color-placeholder);
  font-size: 11px;
}

.info-sidebar__footer {
  padding: 10px 4px 0;
}

.info-sidebar--collapsed .info-sidebar__footer {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 100%;
  padding: 0;
  border-top: 1px solid rgba(31, 35, 41, .08);
}

.sidebar-user {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 42px;
  gap: 9px;
  padding: 4px 8px;
  border-radius: 4px;
  color: var(--td-text-color-primary);
  text-align: left;
}

.info-sidebar--collapsed .sidebar-user {
  justify-content: center;
  width: 40px;
  min-height: 40px;
  padding: 0;
}

.sidebar-user:hover {
  background: var(--td-bg-color-container-hover);
}

.sidebar-user__avatar {
  display: inline-grid;
  flex: 0 0 28px;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  color: var(--td-text-color-anti, #fff);
  background: var(--td-brand-color);
  font-size: 11px;
  font-weight: 600;
}
.sidebar-history__actions { display: none; flex: 0 0 auto; gap: 2px; }
.sidebar-history__item:hover .sidebar-history__actions { display: inline-flex; }
.sidebar-history__actions button { display: inline-grid; place-items: center; width: 22px; height: 22px; padding: 0; border: 0; border-radius: 4px; color: var(--td-text-color-placeholder); background: transparent; cursor: pointer; }
.sidebar-history__actions button:hover { color: var(--td-text-color-primary); background: var(--td-bg-color-container-hover); }
.sidebar-history__actions svg { width: 13px; height: 13px; }
.sidebar-user__avatar img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; }

.info-sidebar--collapsed .sidebar-user__avatar {
  width: 34px;
  height: 34px;
  font-size: 12px;
}

.sidebar-user__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
}

.sidebar-user__chevron {
  flex: 0 0 15px;
  width: 15px;
  height: 15px;
  margin-left: auto;
  color: var(--td-text-color-placeholder);
}

.sidebar-user-menu {
  position: absolute;
  z-index: 30;
  right: 4px;
  bottom: 56px;
  width: 252px;
  padding: 8px;
  border: 1px solid rgba(31, 35, 41, .08);
  border-radius: 14px;
  background: var(--td-bg-color-container);
  box-shadow: 0 12px 28px rgba(31, 35, 41, .14), 0 2px 6px rgba(31, 35, 41, .06);
}

.sidebar-user-menu__item {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 42px;
  gap: 12px;
  padding: 0 11px;
  border: 0;
  border-radius: 9px;
  color: var(--td-text-color-primary);
  background: transparent;
  text-align: left;
  font-size: 14px;
}

.sidebar-user-menu__item:hover {
  background: var(--td-bg-color-secondarycontainer);
}

.sidebar-user-menu__item :deep(svg) {
  width: 18px;
  height: 18px;
  color: var(--td-text-color-secondary);
}

.sidebar-user-menu__item--danger {
  color: var(--td-error-color);
}

.sidebar-user-menu__item--danger :deep(svg) {
  color: var(--td-error-color);
}

.info-sidebar button:focus-visible {
  outline: 2px solid var(--td-brand-color);
  outline-offset: 1px;
}

@media (max-width: 700px) {
  .sidebar-user-menu {
    right: 8px;
    width: min(252px, calc(100vw - 32px));
  }
}

</style>
