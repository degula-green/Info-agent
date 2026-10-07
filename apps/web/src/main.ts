import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TDesign from 'tdesign-vue-next'
import App from './App.vue'
import router from './router'
import './assets/fonts.css'
import 'tdesign-vue-next/dist/tdesign.css'
import './assets/theme/theme.css'
import './style.css'
import { installAuthExpiryHandler } from './auth/expiry'
import { initializeAuthSession } from './auth/session'
import { installDesktopDeepLinkHandler } from './desktop-deep-links'

const app = createApp(App)
app.use(TDesign)
app.use(createPinia())
initializeAuthSession()
installAuthExpiryHandler(router)
installDesktopDeepLinkHandler(router)
app.use(router)
router.isReady().then(() => app.mount('#app'))
