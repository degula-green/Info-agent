import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TDesign from 'tdesign-vue-next'
import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler } from './api/http'
import { setCoreUnauthorizedHandler } from './api/core-auth'
import './assets/fonts.css'
import 'tdesign-vue-next/dist/tdesign.css'
import './assets/theme/theme.css'
import './style.css'

// Any API layer that hits a hard 401 after a failed refresh routes the user
// back to the login page instead of silently continuing unauthenticated.
setUnauthorizedHandler(() => {
  const current = router.currentRoute.value
  if (current?.name === 'login' || current?.name === 'register') return
  void router.push({ name: 'login', query: { redirect: current?.fullPath || '/' } })
})

setCoreUnauthorizedHandler(() => {
  const current = router.currentRoute.value
  if (current?.name === 'login' || current?.name === 'register') return
  void router.push({ name: 'login', query: { redirect: current?.fullPath || '/' } })
})

const app = createApp(App)
app.use(TDesign)
app.use(createPinia())
app.use(router)
router.isReady().then(() => app.mount('#app'))
