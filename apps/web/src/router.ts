import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'login', component: () => import('./views/info/InfoAuthPage.vue') },
    { path: '/register', name: 'register', component: () => import('./views/info/InfoAuthPage.vue') },
    { path: '/', component: () => import('./views/info/InfoShell.vue'), meta: { requiresAuth: true }, children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('./views/info/InfoDashboard.vue') },
      { path: 'search', name: 'search', component: () => import('./views/info/InfoSearchPage.vue') },
      { path: 'knowledge', name: 'knowledge', component: () => import('./views/info/InfoKnowledgeHome.vue') },
      { path: 'knowledge/:platform', name: 'knowledgePlatform', component: () => import('./views/info/InfoKnowledgePage.vue') },
      { path: 'knowledge/:platform/conversations/:conversationId', name: 'conversation', component: () => import('./views/info/InfoConversationPage.vue') },
      { path: 'chat', name: 'chat', component: () => import('./views/info/InfoQuickQAPage.vue') },
      { path: 'profile', name: 'profile', component: () => import('./views/info/InfoProfilePage.vue') },
    ] },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

router.beforeEach((to) => {
  const store = useAuthStore()
  if (to.meta.requiresAuth && !store.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if ((to.name === 'login' || to.name === 'register') && store.isAuthenticated) return '/chat'
  return true
})

export default router
