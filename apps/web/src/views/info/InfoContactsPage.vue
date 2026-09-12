<template>
  <section class="contacts-page">
    <div class="contacts-head"><div><h1>联系人列表</h1><p>管理已接入的微信和飞书联系人</p></div><div class="filters"><t-select v-model="platform" clearable placeholder="全部平台" :options="platformOptions" /></div></div>
    <t-alert v-if="error" theme="error" :message="error" />
    <div v-if="loading" class="state">正在加载联系人…</div>
    <div v-else-if="!contacts.length" class="state empty"><t-icon name="usergroup" /><strong>暂无联系人</strong><span>接入微信或飞书联系人后，他们会显示在这里</span></div>
    <div v-else class="contact-grid"><article v-for="contact in contacts" :key="contact.id" class="contact-card"><div class="avatar">{{ (contact.display_name || contact.identities[0]?.display_name || '?').slice(0,1) }}</div><div class="contact-main"><h3>{{ contact.display_name || contact.identities[0]?.display_name || '未命名联系人' }}</h3><span class="kind">{{ contact.kind === 'internal' ? '内部联系人' : '外部联系人' }}</span><div class="identity-list"><span v-for="identity in contact.identities" :key="identity.id" class="identity"><t-icon :name="identity.platform === 'feishu' ? 'logo-feishu' : 'logo-wechat'" />{{ identity.platform === 'feishu' ? '飞书' : '微信' }} · {{ identity.external_user_id }}</span></div></div></article></div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { listContacts, type ContactDTO } from '@/api/contacts'
const contacts = ref<ContactDTO[]>([]); const loading = ref(false); const error = ref(''); const platform = ref('')
const platformOptions = [{ label: '微信', value: 'wechat' }, { label: '飞书', value: 'feishu' }]
async function load() { loading.value = true; error.value = ''; try { contacts.value = await listContacts(platform.value) } catch (e: any) { error.value = e?.message || '联系人加载失败' } finally { loading.value = false } }
watch(platform, load); onMounted(load)
</script>
<style scoped>
.contacts-page { padding: 28px 34px; max-width: 1180px; margin: auto; }.contacts-head { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:24px; }.contacts-head h1 { margin:0 0 8px; font-size:24px; }.contacts-head p { margin:0; color:var(--td-text-color-secondary); }.filters { width:160px; }.contact-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }.contact-card { display:flex; gap:14px; padding:18px; border:1px solid var(--td-component-stroke); border-radius:12px; background:var(--td-bg-color-container); }.avatar { flex:0 0 42px; width:42px; height:42px; display:grid; place-items:center; border-radius:50%; color:#fff; background:var(--td-brand-color); font-weight:600; }.contact-main { min-width:0; }.contact-main h3 { margin:0 0 4px; font-size:16px; }.kind { color:var(--td-text-color-secondary); font-size:12px; }.identity-list { display:grid; gap:5px; margin-top:12px; }.identity { color:var(--td-text-color-secondary); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.state { padding:80px 0; text-align:center; color:var(--td-text-color-secondary); }.empty { display:grid; gap:10px; place-items:center; }.empty svg { width:36px; height:36px; color:var(--td-brand-color); } @media(max-width:700px){.contacts-page{padding:20px 16px}.contacts-head{display:block}.filters{margin-top:14px;width:100%}}
</style>
