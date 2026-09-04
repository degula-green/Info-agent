<template>
  <div class="wk-login">
    <div class="wk-login__brand"><img src="../assets/img/weknora.png" alt="WeKnora" /><span>Info Agent</span></div>
    <div class="wk-login__showcase"><div class="wk-login__showcase-inner"><div class="wk-login__network"><span v-for="n in 8" :key="n" :class="`network-node network-node--${n}`"><t-icon :name="n % 2 ? 'chat' : 'file'" /></span><span class="network-core"><t-icon name="search" /></span></div><h1>信息管理 Agent</h1><p>让跨平台对话成为可搜索、可追溯的私人知识。</p><div class="wk-login__chips"><span>飞书</span><span>企业微信</span><span>个人微信</span></div></div></div>
    <div class="wk-login__form-wrap"><div class="wk-login__form-card"><div class="wk-login__tabs"><button :class="{ active: mode === 'login' }" @click="mode = 'login'">登录</button><button :class="{ active: mode === 'register' }" @click="mode = 'register'">注册</button></div><div class="wk-login__heading"><h2>{{ mode === 'login' ? '欢迎回来' : '创建账号' }}</h2><p>{{ mode === 'login' ? '使用邮箱和密码进入工作台' : '无需验证码，注册后即可登录' }}</p></div><t-form v-if="mode === 'login'" :data="loginForm" @submit="submitLogin"><t-form-item label="邮箱" name="email"><t-input v-model="loginForm.email" placeholder="请输入邮箱" /></t-form-item><t-form-item label="密码" name="password"><t-input v-model="loginForm.password" type="password" placeholder="请输入密码" /></t-form-item><t-alert v-if="error" theme="error" :message="error" /> <t-button theme="primary" block size="large" type="submit" :loading="submitting">登录</t-button></t-form><t-form v-else :data="registerForm" @submit="submitRegister"><t-form-item label="用户名" name="username"><t-input v-model="registerForm.username" placeholder="请输入用户名" /></t-form-item><t-form-item label="邮箱" name="email"><t-input v-model="registerForm.email" placeholder="请输入邮箱" /></t-form-item><t-form-item label="密码" name="password"><t-input v-model="registerForm.password" type="password" placeholder="至少 6 位" /></t-form-item><t-form-item label="确认密码" name="confirm"><t-input v-model="registerForm.confirm" type="password" placeholder="再次输入密码" /></t-form-item><t-alert v-if="error" theme="error" :message="error" /><t-button theme="primary" block size="large" type="submit" :loading="submitting">注册</t-button></t-form><p v-if="notice" class="wk-login__notice">{{ notice }}</p></div><small class="wk-login__foot">账号信息由服务端安全保存</small></div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ initialMode?: 'login' | 'register' }>()
const emit = defineEmits<{ (event: 'success', nickname: string, email: string): void; (event: 'registered', nickname: string, email: string): void }>()
const mode = ref<'login' | 'register'>(props.initialMode ?? 'login'); const error = ref(''); const notice = ref(''); const submitting = ref(false); const loginForm = ref({ email: '', password: '' }); const registerForm = ref({ username: '', email: '', password: '', confirm: '' })
watch(() => props.initialMode, (value) => { if (value) mode.value = value })
function waitForFeedback() { return new Promise((resolve) => window.setTimeout(resolve, 320)) }
async function submitLogin() {
  error.value = ''
  if (!loginForm.value.email || !loginForm.value.password) { error.value = '请输入邮箱和密码'; return }
  submitting.value = true
  await waitForFeedback()
  const email = loginForm.value.email.trim()
  submitting.value = false
  emit('success', email.split('@')[0], email)
}
async function submitRegister() {
  error.value = ''; notice.value = ''
  const form = registerForm.value
  if (!form.username || !form.email || !form.password || !form.confirm) { error.value = '请完整填写注册信息'; return }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email)) { error.value = '请输入正确的邮箱格式'; return }
  if (form.password !== form.confirm) { error.value = '两次输入的密码不一致'; return }
  submitting.value = true
  await waitForFeedback()
  submitting.value = false
  notice.value = '注册成功，请登录'
  emit('registered', form.username, form.email)
}
</script>

<style lang="less" scoped>
.wk-login { display: flex; min-height: 100vh; color: var(--td-text-color-primary); background: var(--td-bg-color-page); }.wk-login__brand { position: absolute; z-index: 2; top: 25px; left: 34px; display: flex; align-items: center; gap: 8px; color: #fff; font-size: 16px; font-weight: 600; }.wk-login__brand img { width: 25px; filter: brightness(0) invert(1); }.wk-login__showcase { position: relative; display: flex; flex: 1; align-items: center; justify-content: center; overflow: hidden; min-width: 0; color: #fff; background: linear-gradient(145deg, #062d20 0%, #04744a 55%, #07c160 100%); }.wk-login__showcase::before { content: ''; position: absolute; inset: 0; opacity: .17; background-image: radial-gradient(circle at 1px 1px, #fff 1px, transparent 0); background-size: 28px 28px; }.wk-login__showcase-inner { position: relative; width: min(540px, 80%); text-align: center; }.wk-login__showcase h1 { margin: 22px 0 10px; color: #fff; font-size: 32px; font-weight: 600; }.wk-login__showcase p { margin: 0; color: #dcf8e9; font-size: 15px; line-height: 1.7; }.wk-login__chips { display: flex; justify-content: center; gap: 8px; margin-top: 28px; }.wk-login__chips span { padding: 6px 13px; border: 1px solid #ffffff45; border-radius: 16px; color: #e5fff1; background: #ffffff16; font-size: 12px; }.wk-login__network { position: relative; height: 170px; }.network-node, .network-core { position: absolute; display: grid; place-items: center; width: 38px; height: 38px; border: 1px solid #ffffff50; border-radius: 9px; color: #eafff4; background: #ffffff17; }.network-core { top: 64px; left: calc(50% - 25px); width: 50px; height: 50px; border-radius: 50%; border-color: #fff8; background: #ffffff2a; }.network-node--1 { top: 18px; left: 17%; }.network-node--2 { top: 105px; left: 24%; }.network-node--3 { top: 15px; right: 17%; }.network-node--4 { top: 108px; right: 23%; }.network-node--5 { top: 64px; left: 4%; }.network-node--6 { top: 64px; right: 4%; }.network-node--7 { top: 143px; left: 44%; }.network-node--8 { top: 4px; left: 47%; }.network-node svg, .network-core svg { width: 18px; }.wk-login__form-wrap { display: flex; flex: 0 0 430px; flex-direction: column; align-items: center; justify-content: center; padding: 36px; background: #fff; }.wk-login__form-card { width: min(100%, 330px); }.wk-login__tabs { display: flex; gap: 24px; margin-bottom: 32px; border-bottom: 1px solid var(--td-component-stroke); }.wk-login__tabs button { border: 0; border-bottom: 2px solid transparent; padding: 0 0 11px; color: var(--td-text-color-secondary); background: transparent; font-size: 14px; cursor: pointer; }.wk-login__tabs button.active { color: var(--td-brand-color); border-color: var(--td-brand-color); font-weight: 600; }.wk-login__heading { margin-bottom: 25px; }.wk-login__heading h2 { margin: 0 0 7px; font-size: 24px; font-weight: 600; }.wk-login__heading p { margin: 0; color: var(--td-text-color-secondary); font-size: 13px; }.wk-login__form-card :deep(.t-form__item) { margin-bottom: 17px; }.wk-login__form-card :deep(.t-form__label) { font-size: 13px; }.wk-login__form-card :deep(.t-button) { margin-top: 8px; }.wk-login__notice { margin: 14px 0 0; color: var(--td-success-color); font-size: 12px; }.wk-login__foot { display: block; margin-top: 31px; color: var(--td-text-color-placeholder); text-align: center; font-size: 11px; }
@media (max-width: 820px) { .wk-login__showcase { display: none; }.wk-login__brand { color: var(--td-text-color-primary); }.wk-login__brand img { filter: none; }.wk-login__form-wrap { flex: 1; padding: 30px 22px; } }
</style>
