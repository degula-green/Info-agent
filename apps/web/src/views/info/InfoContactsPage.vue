<template>
  <section class="contacts-page">
    <div class="contacts-head"><div><h1>联系人列表</h1><p>管理已接入的微信和飞书联系人</p></div><div class="actions"><t-select v-model="platform" clearable placeholder="全部平台" :options="platformOptions" /><t-button theme="primary" @click="showAttach = true">接入联系人</t-button></div></div>
    <t-alert v-if="error" theme="error" :message="error" closeable @close="error = ''" />
    <div v-if="loading" class="state">正在加载联系人…</div>
    <div v-else-if="!contacts.length" class="state empty"><t-icon name="usergroup" /><strong>暂无联系人</strong><span>接入微信或飞书联系人后，他们会显示在这里</span></div>
    <div v-else class="contact-grid"><article v-for="contact in contacts" :key="contact.id" class="contact-card" @click="openDetail(contact)"><div class="avatar">{{ (contact.display_name || contact.identities[0]?.display_name || '?').slice(0,1) }}</div><div class="contact-main"><h3>{{ contact.display_name || contact.identities[0]?.display_name || '未命名联系人' }}</h3><span class="kind">{{ contact.kind === 'internal' ? '内部联系人' : '外部联系人' }}</span><div class="identity-list"><span v-for="identity in contact.identities" :key="identity.id" class="identity"><t-icon :name="identity.platform === 'feishu' ? 'logo-feishu' : 'logo-wechat'" />{{ identity.platform === 'feishu' ? '飞书' : '微信' }} · {{ identity.external_user_id }}</span></div><div class="contact-stats">消息 {{ contact.message_count || 0 }} · 附件 {{ contact.attachment_count || 0 }}</div></div></article></div>
    <t-dialog v-model:visible="showAttach" header="接入联系人" :confirm-btn="null" width="620px"><t-space direction="vertical" style="width:100%"><t-alert v-if="attachPlatform === 'feishu' && !feishuBound" theme="warning" title="需要先绑定飞书" message="请先到个人中心完成飞书授权，再返回这里选择联系人。" closable="false" /><t-select v-model="attachPlatform" :options="platformOptions" placeholder="选择平台账号" @change="discover" /><t-input v-model="query" placeholder="搜索姓名或账号" clearable @enter="discover" /><t-loading :loading="discovering"><div class="discover-list"><div v-for="item in visibleAvailable" :key="item.external_user_id" class="discover-item"><div class="discover-item__identity"><strong>{{ item.display_name || item.external_user_id }}</strong><small>{{ item.external_user_id }}<template v-if="item.email"> · {{ item.email }}</template></small><small v-if="item.department || item.job_title">{{ [item.department, item.job_title].filter(Boolean).join(' · ') }}</small></div><t-button size="small" :disabled="item.selected" @click="attach(item)">{{ item.selected ? '已接入' : '接入' }}</t-button></div><t-button v-if="visibleAvailable.length < available.length" class="discover-more" variant="text" @click="visibleContactCount += 100">继续加载（{{ available.length - visibleAvailable.length }}）</t-button><span v-if="!available.length && !discovering" class="discover-empty">{{ attachPlatform === 'feishu' && !feishuBound ? '绑定飞书后可发现联系人' : '暂无可接入联系人' }}</span></div></t-loading></t-space></t-dialog>
    <t-dialog v-model:visible="showDetail" header="联系人详情" :confirm-btn="null" width="min(760px, calc(100vw - 32px))" dialog-class-name="contact-detail-dialog" placement="center">
      <div v-if="detail" class="contact-detail" :aria-busy="detailLoading">
        <div class="contact-detail__identity">
          <div class="avatar">{{ (detail.display_name || detail.identities?.[0]?.display_name || '?').slice(0, 1) }}</div>
          <div><h2>{{ detail.display_name || '未命名联系人' }}</h2><span>{{ detail.kind === 'internal' ? `内部联系人 · ${detail.internal_user_id || ''}` : '外部联系人' }}</span></div>
        </div>
        <div class="contact-detail__identities"><span v-for="identity in detail.identities" :key="identity.id">{{ identity.platform === 'feishu' ? '飞书' : '微信' }} · {{ identity.display_name || identity.external_user_id }}</span></div>
        <div v-if="detailLoading" class="contact-detail__loading"><t-icon name="loading" />正在加载消息和附件…</div>
        <template v-else>
          <section class="contact-detail__section"><h3>相关消息 <small>{{ visibleDetailMessages.length }}</small></h3><div v-if="visibleDetailMessages.length" class="contact-message-list"><article v-for="message in visibleDetailMessages" :key="message.id" class="contact-message"><div><strong>{{ message.sender_display_name || '未知发送人' }}</strong><time>{{ formatDate(message.sent_at) }}</time></div><p>{{ messageContent(message.content) }}</p></article></div><span v-else class="contact-detail__empty">暂无已采集消息</span></section>
          <section class="contact-detail__section"><h3>相关附件 <small>{{ detail.attachments?.length || 0 }}</small></h3><div v-if="detail.attachments?.length" class="contact-attachment-list"><button v-for="attachment in detail.attachments" :key="attachment.id" type="button" class="contact-attachment" @click="openAttachment(attachment)"><t-icon name="file" /><span><strong>{{ attachment.file_name }}</strong><small>{{ formatSize(attachment.size_bytes) }} · {{ attachment.mime_type || '未知类型' }}</small></span><t-icon name="chevron-right" /></button></div><span v-else class="contact-detail__empty">暂无已采集附件</span></section>
        </template>
        <div class="contact-detail__actions"><t-button theme="danger" variant="outline" :disabled="detailLoading" @click="remove">移除联系人</t-button></div>
      </div>
    </t-dialog>
    <t-dialog v-model:visible="previewVisible" header="附件预览" :footer="false" width="min(92vw, 1200px)" dialog-class-name="contact-attachment-preview-dialog" placement="center" destroy-on-close><div class="contact-attachment-preview"><InfoAttachmentPreview v-if="previewFile" :file="previewFile" :active="previewVisible" /></div></t-dialog>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { listContacts, discoverContacts, attachContact, removeContact, getContact, type ContactDTO, type AvailableContactDTO, type ContactDetailDTO } from '@/api/contacts'
import { getConnectors, type AttachmentDTO, type ConnectorDTO } from '@/api/info-knowledge'
import InfoAttachmentPreview from '@/components/InfoAttachmentPreview.vue'
import type { InfoFile } from '@/mock'
import { isDisplayableTextMessage } from '@/utils/message-visibility'
const contacts = ref<ContactDTO[]>([]); const loading = ref(false); const error = ref(''); const platform = ref(''); const showAttach = ref(false); const attachPlatform = ref<'wechat'|'feishu'>('wechat'); const query = ref(''); const available = ref<AvailableContactDTO[]>([]); const discovering = ref(false); const showDetail = ref(false); const detail = ref<ContactDetailDTO | null>(null); const detailLoading = ref(false); let detailRequest = 0
const previewVisible = ref(false); const previewFile = ref<InfoFile | null>(null)
const connectors = ref<ConnectorDTO[]>([])
const visibleContactCount = ref(100)
const visibleAvailable = computed(() => available.value.slice(0, visibleContactCount.value))
const visibleDetailMessages = computed(() => (detail.value?.messages || []).filter((message) => {
  return isDisplayableTextMessage(message.message_type, message.content)
}))
const feishuBound = computed(() => connectors.value.some((item) => item.platform === 'feishu' && item.bound && item.status === 'active'))
const platformOptions = [{ label: '微信', value: 'wechat' }, { label: '飞书', value: 'feishu' }]
async function load() { loading.value = true; error.value = ''; try { contacts.value = await listContacts(platform.value) } catch (e: any) { error.value = e?.message || '联系人加载失败' } finally { loading.value = false } }
async function refreshDetail(contactID: string, showLoading = false) {
  const request = ++detailRequest
  if (showLoading) detailLoading.value = true
  try {
    const loaded = await getContact(contactID)
    if (request === detailRequest) detail.value = loaded
  } catch (e: any) {
    if (request === detailRequest && showLoading) error.value = e?.message || '联系人详情加载失败'
  } finally {
    if (request === detailRequest && showLoading) detailLoading.value = false
  }
}
let contactPollTimer: number | undefined
watch(platform, load); watch(attachPlatform, () => { if (showAttach.value) void discover() }); watch(showAttach, (visible) => { if (visible) { void refreshConnectors(); void discover() } }); onMounted(async () => { await Promise.all([load(), refreshConnectors()]); contactPollTimer = window.setInterval(() => { if (detail.value && showDetail.value) void refreshDetail(detail.value.id); else void load() }, 10000) })
onBeforeUnmount(() => { if (contactPollTimer != null) window.clearInterval(contactPollTimer) })
async function refreshConnectors() { try { connectors.value = await getConnectors() } catch { connectors.value = [] } }
async function discover() { discovering.value = true; visibleContactCount.value = 100; error.value = ''; try { available.value = await discoverContacts(attachPlatform.value, query.value) } catch (e:any) { available.value = []; error.value = e?.message || '联系人发现失败' } finally { discovering.value = false } }
async function attach(item: AvailableContactDTO) { if (item.selected) return; try { await attachContact({ platform: attachPlatform.value, externalUserID: item.external_user_id, displayName: item.display_name, avatarURL: item.avatar_url }); item.selected = true; await load() } catch (e:any) { error.value = e?.message || '联系人接入失败' } }
async function openDetail(contact: ContactDTO) {
  detail.value = { ...contact, messages: [], attachments: [] }
  showDetail.value = true
  await refreshDetail(contact.id, true)
}
async function remove() { if (!detail.value) return; try { await removeContact(detail.value.id); showDetail.value = false; detail.value = null; await load() } catch (e:any) { error.value = e?.message || '联系人移除失败' } }
function formatDate(value?: string) { if (!value) return ''; return new Date(value).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' }) }
function formatSize(bytes?: number) { if (!bytes) return '大小未知'; if (bytes < 1024) return `${bytes} B`; if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`; return `${(bytes / (1024 * 1024)).toFixed(1)} MB` }
function messageContent(content?: string) { const value = String(content || '').trim().replace(/^(?:wxid_[A-Za-z0-9_-]+(?:@chatroom)?|[A-Za-z0-9_-]+@chatroom)\s*:\s*/i, ''); if (!/^<(?:\?xml|msg|appmsg)\b/i.test(value)) return value; const fields = ['title', 'des', 'description'].map((tag) => value.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)</${tag}>`, 'i'))?.[1]?.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()).filter(Boolean); return fields.join('\n') || '结构化消息（无可显示正文）' }
function openAttachment(value: AttachmentDTO) { previewFile.value = { id: value.id, name: value.file_name || '附件', type: value.file_name?.split('.').pop() || value.mime_type || 'FILE', mimeType: value.mime_type || '', size: formatSize(value.size_bytes), time: formatDate(value.created_at), uploadedAt: formatDate(value.created_at), uploader: '', content: '', timestamp: value.created_at, documentStatus: value.content_status, parseStatus: value.content_status, previewCapability: value.preview_capability, contentAccessRequired: value.content_access_required, fileSizeBytes: value.size_bytes }; previewVisible.value = true }
</script>
<style scoped>
.contacts-page { padding: 28px 34px; max-width: 1180px; margin: auto; }.contacts-head { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:24px; }.contacts-head h1 { margin:0 0 8px; font-size:24px; }.contacts-head p { margin:0; color:var(--td-text-color-secondary); }.actions { display:flex; gap:10px; width:320px; }.actions .t-select { flex:1; }.contact-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:14px; }.contact-card { display:flex; gap:14px; padding:18px; border:1px solid var(--td-component-stroke); border-radius:12px; background:var(--td-bg-color-container); cursor:pointer; }.avatar { flex:0 0 42px; width:42px; height:42px; display:grid; place-items:center; border-radius:50%; color:#fff; background:var(--td-brand-color); font-weight:600; }.contact-main { min-width:0; }.contact-main h3 { margin:0 0 4px; font-size:16px; }.kind { color:var(--td-text-color-secondary); font-size:12px; }.identity-list { display:grid; gap:5px; margin-top:12px; }.identity { color:var(--td-text-color-secondary); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.discover-list { display:grid; gap:8px; max-height:360px; overflow:auto; }.discover-item { display:flex; align-items:center; gap:10px; padding:8px; border-bottom:1px solid var(--td-component-stroke); }.discover-item__identity { display:grid; flex:1; min-width:0; gap:3px; }.discover-item strong, .discover-item small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.discover-item small { color:var(--td-text-color-secondary); }.discover-empty { padding:24px 0; color:var(--td-text-color-secondary); text-align:center; }.state { padding:80px 0; text-align:center; color:var(--td-text-color-secondary); }.empty { display:grid; gap:10px; place-items:center; }.empty svg { width:36px; height:36px; color:var(--td-brand-color); } @media(max-width:700px){.contacts-page{padding:20px 16px}.contacts-head{display:block}.actions{margin-top:14px;width:100%}}
.contact-detail { display:grid; gap:18px; }.contact-detail__identity { display:flex; align-items:center; gap:12px; }.contact-detail__identity h2 { margin:0 0 4px; font-size:20px; }.contact-detail__identity span, .contact-detail__identities, .contact-detail__empty { color:var(--td-text-color-secondary); font-size:12px; }.contact-detail__identities { display:flex; flex-wrap:wrap; gap:8px; }.contact-detail__identities span { padding:5px 9px; border:1px solid var(--td-component-stroke); border-radius:6px; }.contact-detail__section { display:grid; gap:9px; }.contact-detail__section h3 { margin:0; font-size:14px; }.contact-detail__section h3 small { margin-left:5px; color:var(--td-text-color-secondary); font-size:12px; font-weight:400; }.contact-message-list, .contact-attachment-list { display:grid; gap:8px; max-height:250px; overflow:auto; }.contact-message { padding:10px 12px; border:1px solid var(--td-component-stroke); border-radius:6px; }.contact-message > div { display:flex; justify-content:space-between; gap:10px; }.contact-message time, .contact-message p { color:var(--td-text-color-secondary); font-size:12px; }.contact-message p { margin:7px 0 0; white-space:pre-wrap; }.contact-attachment { display:flex; align-items:center; gap:10px; width:100%; padding:10px 12px; border:1px solid var(--td-component-stroke); border-radius:6px; color:var(--td-text-color-primary); background:transparent; text-align:left; cursor:pointer; }.contact-attachment > span { flex:1; min-width:0; }.contact-attachment strong, .contact-attachment small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.contact-attachment small { margin-top:3px; color:var(--td-text-color-secondary); }.contact-detail__actions { display:flex; justify-content:flex-end; }
.contact-detail { max-height:min(680px, calc(100dvh - 170px)); overflow:hidden; }
.contact-detail__section { min-height:0; }
.contact-message-list, .contact-attachment-list { max-height:190px; }
.contact-stats { margin-top:10px; color:var(--td-text-color-placeholder); font-size:11px; }
 .contact-detail__loading { display:flex; min-height:128px; align-items:center; justify-content:center; gap:8px; color:var(--td-text-color-secondary); }
 .discover-more { width:100%; margin-top:4px; }
:global(.t-dialog__wrap:has(.contact-detail-dialog) .t-dialog__position) { display:flex; min-height:100%; height:100%; align-items:center; justify-content:center; box-sizing:border-box; }
.contact-attachment-preview { display:flex; min-width:0; height:min(calc(100dvh - 150px), 760px); max-height:calc(100dvh - 150px); overflow:hidden; }
.contact-attachment-preview :deep(.attachment-preview) { min-height:0; flex:1; }
:global(.contact-attachment-preview-dialog.t-dialog) { max-height:calc(100dvh - 32px); margin:0 auto; overflow:hidden; }
:global(.contact-attachment-preview-dialog .t-dialog__body) { min-height:0; max-height:calc(100dvh - 112px); overflow:hidden; }
:global(.t-dialog__wrap:has(.contact-attachment-preview-dialog)) { overflow:hidden; }
:global(.t-dialog__wrap:has(.contact-attachment-preview-dialog) .t-dialog__position) { display:flex; min-height:100%; height:100%; align-items:center; justify-content:center; box-sizing:border-box; }
</style>
