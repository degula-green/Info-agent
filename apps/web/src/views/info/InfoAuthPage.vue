<template>
  <InfoAuth :initial-mode="mode" @success="handleLogin" @registered="handleRegister" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MessagePlugin } from 'tdesign-vue-next'
import InfoAuth from '../../components/InfoAuth.vue'
import { useInfoMockStore } from '../../stores/infoMock'

const route = useRoute()
const router = useRouter()
const auth = useInfoMockStore()
const mode = computed(() => route.path === '/register' ? 'register' : 'login')

function handleLogin(nickname: string, email: string) {
  // Core 返回的令牌不携带昵称；注册后暂存的昵称用于同步现有资料状态。
  const pendingNickname = sessionStorage.getItem('info_agent_pending_nickname') || nickname
  auth.login(email, pendingNickname || undefined)
  sessionStorage.removeItem('info_agent_pending_nickname')
  router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : '/chat')
}

function handleRegister(nickname: string, _email: string) {
  sessionStorage.setItem('info_agent_pending_nickname', nickname)
  MessagePlugin.success('注册成功，请登录')
  router.replace('/login')
}
</script>
