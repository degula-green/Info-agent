<template>
  <InfoAuth :initial-mode="mode" @success="handleLogin" @registered="handleRegister" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import InfoAuth from '../../components/InfoAuth.vue'
import { useInfoMockStore } from '../../stores/infoMock'

const route = useRoute()
const router = useRouter()
const auth = useInfoMockStore()
const mode = computed(() => route.path === '/register' ? 'register' : 'login')

function handleLogin(nickname: string, email: string) {
  auth.login(email, nickname)
  router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : '/chat')
}

function handleRegister(_nickname: string, _email: string) {
  router.replace('/login')
}
</script>
