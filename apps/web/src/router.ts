import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'login', component: () => import('./views/info/InfoAuthPage.vue') },
    { path: '/register', name: 'register', component: () => import('./views/info/InfoAuthPage.vue') },
    { path: '/organization-invitations/:token', name: 'organizationInvitation', component: () => import('./views/info/InfoProfilePage.vue'), meta: { requiresAuth: true } },
    { path: '/', component: () => import('./views/info/InfoShell.vue'), meta: { requiresAuth: true }, children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('./views/info/InfoDashboard.vue') },
      { path: 'search', name: 'search', component: () => import('./views/info/InfoSearchPage.vue') },
      { path: 'knowledge', name: 'knowledge', component: () => import('./views/info/InfoKnowledgeHome.vue') },
      { path: 'knowledge/organization/files', name: 'knowledgeOrganizationFiles', component: () => import('./views/info/InfoKnowledgeLibraryPage.vue'), props: { libraryKind: 'organization_files' } },
      { path: 'knowledge/organization/groups', name: 'knowledgeOrganizationGroups', component: () => import('./views/info/InfoKnowledgeLibraryPage.vue'), props: { libraryKind: 'organization_conversation' } },
      { path: 'knowledge/organization/private-shared', name: 'knowledgeOrganizationPrivateShared', component: () => import('./views/info/InfoKnowledgeLibraryPage.vue'), props: { libraryKind: 'organization_private_shared' } },
      { path: 'knowledge/personal/private', name: 'knowledgePersonalPrivate', component: () => import('./views/info/InfoKnowledgeLibraryPage.vue'), props: { libraryKind: 'private_conversation' } },
      { path: 'knowledge/personal/files', name: 'knowledgePersonalFiles', component: () => import('./views/info/InfoKnowledgeLibraryPage.vue'), props: { libraryKind: 'private_local' } },
      { path: 'knowledge/:platform', name: 'knowledgePlatform', component: () => import('./views/info/InfoKnowledgePage.vue') },
      { path: 'knowledge/:platform/conversations/:conversationId', name: 'conversation', component: () => import('./views/info/InfoConversationPage.vue') },
      { path: 'organization', name: 'organization', component: () => import('./views/info/InfoOrganizationPage.vue') },
      { path: 'contacts', name: 'contacts', component: () => import('./views/info/InfoContactsPage.vue') },
      { path: 'chat', name: 'chat', component: () => import('./views/info/InfoQuickQAPage.vue') },
      { path: 'profile', name: 'profile', component: () => import('./views/info/InfoProfilePage.vue') },
    ] },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

router.beforeEach((to) => {
  const store = useAuthStore()
  // Pick up cross-tab logins/logouts and locally-detectable token expiry.
  store.sync()
  if (to.meta.requiresAuth && !store.isAuthenticated) {
    // An expired or missing token must never keep a protected route mounted.
    if (store.accessToken) store.clear()
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if ((to.name === 'login' || to.name === 'register') && store.isAuthenticated) return '/chat'
  return true
})

export default router
