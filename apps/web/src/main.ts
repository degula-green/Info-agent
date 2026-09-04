import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TDesign from 'tdesign-vue-next'
import App from './App.vue'
import router from './router'
import './assets/fonts.css'
import 'tdesign-vue-next/dist/tdesign.css'
import './assets/theme/theme.css'
import './style.css'

const app = createApp(App)
app.use(TDesign)
app.use(createPinia())
app.use(router)
router.isReady().then(() => app.mount('#app'))
