<template>
  <section class="profile-page">
    <div class="profile-hero">
      <button class="profile-hero__avatar" type="button" aria-label="编辑头像" @click="avatarInput?.click()">
        <img v-if="profile.avatar_url" :src="profile.avatar_url" alt="" />
        <span v-else>{{ avatarLabel }}</span>
        <span class="profile-hero__badge"><t-icon name="check-circle-filled" /></span>
      </button>
      <input ref="avatarInput" class="avatar-file-input" type="file" accept="image/jpeg,image/png,image/webp" @change="onAvatarSelected" />
      <h1>{{ form.nickname || '未设置昵称' }}</h1>
      <p>{{ profile.email }}</p>
    </div>

    <div class="profile-layout">
      <section class="profile-panel" aria-labelledby="profile-info-title">
        <div class="profile-panel__heading">
          <span class="profile-panel__icon"><t-icon name="user" /></span>
          <div>
            <h2 id="profile-info-title">个人资料</h2>
            <p>查看和编辑你的基本信息</p>
          </div>
        </div>
        <div class="profile-settings">
          <div class="profile-setting-row">
            <div class="profile-setting-row__label"><t-icon name="user" /><span>昵称</span></div>
            <t-input v-model="form.nickname" class="profile-setting-row__input" placeholder="请输入昵称" aria-label="昵称" />
          </div>
          <div class="profile-setting-row">
            <div class="profile-setting-row__label"><t-icon name="email" /><span>邮箱</span></div>
            <span class="profile-setting-row__value">{{ profile.email }}</span>
          </div>
        </div>
      </section>

      <section class="profile-panel" aria-labelledby="connector-title">
        <div class="profile-panel__heading">
          <span class="profile-panel__icon"><t-icon name="link-1" /></span>
          <div>
            <h2 id="connector-title">连接器</h2>
            <p>绑定后开放对应知识库，数据仅本人可见</p>
          </div>
        </div>
        <div class="connector-list">
          <div v-for="connector in connectors" :key="connector.platform" class="connector-row">
            <span class="connector-mark" :style="{ background: sourceColor[connector.platform] }">{{ connector.display_name.slice(0, 1) }}</span>
            <div class="connector-row__body">
              <strong>{{ connector.display_name }}</strong>
              <small>{{ connectorSummary(connector) }}</small>
            </div>
            <t-tag :theme="connector.bound ? 'success' : 'default'" variant="light">{{ connectorStatus(connector) }}</t-tag>
            <t-button class="connector-action" :theme="connector.bound || connector.cleanup_pending ? 'default' : 'primary'" :variant="connector.bound || connector.cleanup_pending ? 'outline' : 'base'" size="medium" :loading="connectorPending[connector.platform]" :disabled="connector.availability !== 'available' || connectorPending[connector.platform]" @click="handleConnector(connector)">
              {{ connector.cleanup_pending ? '重试解绑' : connector.bound ? '解除绑定' : connector.availability === 'available' ? `绑定${connector.display_name}` : '暂未开放' }}
            </t-button>
          </div>
        </div>
      </section>
    </div>

    <div class="profile-actions">
      <t-button theme="primary" @click="saveProfile">
        <template #icon><t-icon name="check" /></template>
        保存修改
      </t-button>
      <t-button variant="outline" @click="restoreDialogVisible = true">
        <template #icon><t-icon name="refresh" /></template>
        恢复演示数据
      </t-button>
    </div>

    <t-dialog v-model:visible="feishuDialogVisible" header="绑定飞书" :confirm-btn="'前往飞书授权'" :cancel-btn="'取消'" :confirm-loading="feishuBinding" @confirm="confirmFeishuBind">
      <div class="auth-dialog">
        <div class="auth-dialog__icon" :style="{ background: sourceColor.feishu }">飞</div>
        <h3>授权飞书连接器</h3>
        <p>授权后，Info Agent 将读取你有权限访问的群聊、私聊消息和文件。</p>
        <div class="auth-dialog__scope">
          <span><t-icon name="check-circle-filled" />读取会话消息</span>
          <span><t-icon name="check-circle-filled" />读取文件和图片</span>
          <span><t-icon name="lock-on" />数据仅本人可见</span>
        </div>
      </div>
    </t-dialog>
    <t-dialog v-model:visible="wechatDialogVisible" header="绑定个人微信" :confirm-btn="'确认绑定'" :cancel-btn="'取消'" :confirm-loading="wechatBinding" @confirm="confirmWechatBind">
      <t-form :data="wechatForm" label-align="top">
        <t-form-item label="微信 ID"><t-input v-model="wechatForm.wxid" placeholder="例如 wxid_xxx" /></t-form-item>
        <t-form-item label="本机微信数据目录"><t-input v-model="wechatForm.db_dir" placeholder="仅本机开发环境可用" /></t-form-item>
      </t-form>
    </t-dialog>
    <t-dialog v-model:visible="restoreDialogVisible" header="恢复演示数据" :confirm-btn="{ content: '恢复数据', theme: 'danger' }" cancel-btn="取消" @confirm="restoreDemo">
      <p class="restore-dialog__copy">将恢复默认资料、连接器、会话和问答历史，当前本地修改会被覆盖。</p>
    </t-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { sourceColor } from '@/mock'
import { useInfoMockStore } from '@/stores/infoMock'
import { bindWechat, getConnectors, getProfile, type Connector, type ConnectorPlatform, type Profile, unbindConnector, updateProfile, uploadAvatar } from '@/mock-api/info-profile'

const store = useInfoMockStore()
const profile = ref<Profile>({ id: 0, username: '', nickname: store.profile.nickname, email: store.profile.email, avatar_url: null, updated_at: '' })
const form = reactive({ nickname: store.profile.nickname })
const connectors = ref<Connector[]>([])
const avatarInput = ref<HTMLInputElement | null>(null)
const feishuDialogVisible = ref(false)
const wechatDialogVisible = ref(false)
const restoreDialogVisible = ref(false)
const wechatRebind = ref(false)
const wechatForm = reactive({ wxid: '', db_dir: '' })
const connectorPending = reactive<Record<ConnectorPlatform, boolean>>({ feishu: false, wecom: false, wechat: false })
const feishuBinding = ref(false)
const wechatBinding = ref(false)
const avatarLabel = computed(() => (form.nickname.trim().slice(0, 1) || '我'))

function errorMessage(cause: any, fallback: string) { return cause?.message || cause?.error?.message || fallback }
function syncProfile(value: Profile) {
  profile.value = value
  form.nickname = value.nickname
  store.updateProfile({ nickname: value.nickname, email: value.email, avatar: value.nickname.slice(0, 1) || '我' })
}
async function loadPage() {
  try {
    const [nextProfile, nextConnectors] = await Promise.all([getProfile(), getConnectors()])
    syncProfile(nextProfile)
    connectors.value = nextConnectors
  } catch (cause) { MessagePlugin.error(errorMessage(cause, '个人中心加载失败，请重新登录后再试')) }
}
async function saveProfile() {
  try { syncProfile(await updateProfile(form.nickname)); MessagePlugin.success('个人资料已保存') }
  catch (cause) { MessagePlugin.error(errorMessage(cause, '保存失败')) }
}
async function onAvatarSelected(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  try { syncProfile(await uploadAvatar(file)); MessagePlugin.success('头像已更新') }
  catch (cause) { MessagePlugin.error(errorMessage(cause, '头像上传失败')) }
  finally { if (avatarInput.value) avatarInput.value.value = '' }
}
function connectorStatus(connector: Connector) {
  if (connector.availability !== 'available') return '暂未开放'
  if (connector.cleanup_pending) return '待完成解绑'
  return ({ unbound: '未绑定', active: '已绑定', paused: '已暂停', error: '异常', offline: '离线' } as Record<string, string>)[connector.status]
}
function connectorSummary(connector: Connector) {
  if (connector.availability !== 'available') return '该连接器暂未开放'
  if (connector.cleanup_pending) return connector.last_error === 'wechat_stop_failed' ? '采集器尚未停止，请重试解绑' : '认证凭据尚未清理，请重试解绑'
  if (!connector.bound) return `未绑定，绑定后开放${connector.display_name}知识库`
  return connector.account_name || '已绑定，等待同步账号信息'
}
async function refreshConnectors() { connectors.value = await getConnectors() }
async function handleConnector(connector: Connector) {
  if (connectorPending[connector.platform]) return
  if (connector.bound || connector.cleanup_pending) {
    connectorPending[connector.platform] = true
    try { await unbindConnector(connector.platform); await refreshConnectors(); MessagePlugin.success(`已解除${connector.display_name}绑定`) }
    catch (cause) {
      await refreshConnectors().catch(() => undefined)
      MessagePlugin.error(errorMessage(cause, '解除绑定失败，请重试'))
    }
    finally { connectorPending[connector.platform] = false }
    return
  }
  if (connector.platform === 'feishu') feishuDialogVisible.value = true
  if (connector.platform === 'wechat') { wechatRebind.value = false; wechatForm.wxid = ''; wechatForm.db_dir = ''; wechatDialogVisible.value = true }
}
async function confirmFeishuBind() {
  if (feishuBinding.value) return
  feishuBinding.value = true
  try {
    await new Promise((resolve) => window.setTimeout(resolve, 320))
    store.bindSource('feishu')
    feishuDialogVisible.value = false
    await refreshConnectors()
    MessagePlugin.success('飞书已绑定')
  } catch (cause) { MessagePlugin.error(errorMessage(cause, '飞书授权暂不可用')) }
  finally { feishuBinding.value = false }
}
function isAbsoluteLocalPath(value: string) { return /^(?:[a-zA-Z]:[\\/]|\\\\|\/)/.test(value.trim()) }
function normalizedWechatBinding() {
  const wxid = wechatForm.wxid.trim()
  const dbDir = wechatForm.db_dir.trim()
  if (!wxid || !dbDir) { MessagePlugin.warning('请填写微信 ID 和本机微信数据目录'); return null }
  if (isAbsoluteLocalPath(wxid) || !isAbsoluteLocalPath(dbDir)) {
    MessagePlugin.warning('未找到本地微信数据库，请检查微信 ID 和数据目录')
    return null
  }
  return { wxid, db_dir: dbDir }
}
async function confirmWechatBind() {
  if (wechatBinding.value) return
  const input = normalizedWechatBinding()
  if (!input) return
  wechatBinding.value = true
  try {
    await bindWechat(input, wechatRebind.value)
    wechatDialogVisible.value = false
    await refreshConnectors()
    MessagePlugin.success('个人微信已绑定')
  } catch (cause) { MessagePlugin.error(errorMessage(cause, '个人微信绑定失败')) }
  finally { wechatBinding.value = false }
}
async function restoreDemo() {
  store.resetDemo()
  restoreDialogVisible.value = false
  await loadPage()
  MessagePlugin.success('演示数据已恢复')
}
onMounted(loadPage)
</script>

<style lang="less" scoped>
.profile-page {
  width: min(920px, 100%);
  margin: 0 auto;
  padding: 36px 34px 56px;
}

.profile-hero {
  display: flex;
  align-items: center;
  flex-direction: column;
  margin-bottom: 34px;
  text-align: center;
}

.profile-hero__avatar {
  position: relative;
  display: grid;
  place-items: center;
  width: 112px;
  height: 112px;
  margin-bottom: 15px;
  padding: 0;
  border: 5px solid var(--td-bg-color-container);
  border-radius: 50%;
  color: var(--td-text-color-anti, #fff);
  background: var(--td-brand-color);
  box-shadow: 0 4px 16px rgba(31, 35, 41, .12);
  font-size: 38px;
  font-weight: 650;
  cursor: pointer;
}

.profile-hero__avatar:hover {
  box-shadow: 0 5px 19px rgba(31, 35, 41, .18);
}

.profile-hero__avatar img {
  width: 100%;
  height: 100%;
  border-radius: inherit;
  object-fit: cover;
}

.avatar-file-input {
  display: none;
}

.profile-hero__badge {
  position: absolute;
  right: -1px;
  bottom: 1px;
  display: grid;
  place-items: center;
  width: 27px;
  height: 27px;
  border: 3px solid var(--td-bg-color-container);
  border-radius: 50%;
  color: #fff;
  background: var(--td-brand-color);
  font-size: 16px;
}

.profile-hero h1 {
  margin: 0;
  font-size: 27px;
  font-weight: 650;
}

.profile-hero p {
  margin: 7px 0 0;
  color: var(--td-text-color-secondary);
  font-size: 13px;
}

.profile-layout {
  display: grid;
  gap: 20px;
}

.profile-panel {
  overflow: hidden;
  border: 1px solid var(--td-component-stroke);
  border-radius: 14px;
  background: var(--td-bg-color-container);
  box-shadow: 0 5px 18px rgba(31, 35, 41, .05);
}

.profile-panel__heading {
  display: flex;
  align-items: center;
  gap: 17px;
  padding: 25px 36px 24px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.profile-panel__icon {
  display: grid;
  place-items: center;
  flex: 0 0 60px;
  width: 60px;
  height: 60px;
  border-radius: 15px;
  color: var(--td-brand-color);
  background: var(--td-brand-color-light);
  font-size: 27px;
}

.profile-panel__heading h2 {
  margin: 0 0 5px;
  font-size: 19px;
  font-weight: 650;
}

.profile-panel__heading p {
  margin: 0;
  color: var(--td-text-color-secondary);
  font-size: 13px;
}

.profile-settings,
.connector-list {
  display: grid;
}

.profile-setting-row,
.connector-row {
  display: flex;
  align-items: center;
  min-height: 72px;
  padding: 14px 36px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.profile-setting-row:last-child,
.connector-row:last-child {
  border-bottom: 0;
}

.profile-setting-row__label {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 170px;
  color: var(--td-text-color-secondary);
  font-size: 14px;
}

.profile-setting-row__label :deep(svg) {
  width: 18px;
  height: 18px;
}

.profile-setting-row__input {
  width: min(420px, 55%);
  margin-left: auto;
}

.profile-setting-row__input :deep(.t-input),
.profile-setting-row__input :deep(.t-input__inner) {
  border-color: transparent;
  background: transparent;
  text-align: right;
}

.profile-setting-row__value {
  margin-left: auto;
  color: var(--td-text-color-primary);
  font-size: 14px;
}

.connector-row {
  gap: 14px;
  min-height: 84px;
}

.connector-mark {
  display: grid;
  place-items: center;
  flex: 0 0 38px;
  width: 38px;
  height: 38px;
  border-radius: 9px;
  color: #fff;
  font-size: 14px;
  font-weight: 650;
}

.connector-row__body {
  flex: 1;
  min-width: 0;
}

.connector-row__body strong,
.connector-row__body small {
  display: block;
}

.connector-action {
  width: 92px;
  min-width: 92px;
  height: 32px;
  flex: 0 0 92px;
  padding: 0 10px;
  font-size: 12px;
}

.connector-row__body strong {
  font-size: 14px;
}

.connector-row__body small {
  margin-top: 4px;
  overflow: hidden;
  color: var(--td-text-color-secondary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: flex-end;
  margin-top: 20px;
}

.restore-dialog__copy {
  margin: 0;
  color: var(--td-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
}

.avatar-dialog {
  display: flex;
  align-items: center;
  gap: 16px;
}

.avatar-dialog__preview {
  display: grid;
  place-items: center;
  flex: 0 0 56px;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  color: #fff;
  background: var(--td-brand-color);
  font-size: 20px;
  font-weight: 650;
}

.avatar-dialog .t-input {
  flex: 1;
}

.auth-dialog {
  text-align: center;
}

.auth-dialog__icon {
  display: grid;
  place-items: center;
  width: 52px;
  height: 52px;
  margin: 3px auto 14px;
  border-radius: 12px;
  color: #fff;
  font-size: 20px;
  font-weight: 600;
}

.auth-dialog h3 {
  margin: 0 0 8px;
  font-size: 16px;
}

.auth-dialog p {
  max-width: 48ch;
  margin: 0 auto;
  color: var(--td-text-color-secondary);
  font-size: 12px;
  line-height: 1.7;
}

.auth-dialog__scope {
  display: grid;
  gap: 7px;
  margin-top: 17px;
  padding: 12px;
  border-radius: 6px;
  background: var(--td-bg-color-secondarycontainer);
  text-align: left;
}

.auth-dialog__scope span {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--td-text-color-secondary);
  font-size: 11px;
}

.auth-dialog__scope span :deep(svg) {
  width: 14px;
  color: var(--td-brand-color);
}

@media (max-width: 700px) {
  .profile-page {
    padding: 24px 16px 44px;
  }

  .profile-hero {
    margin-bottom: 25px;
  }

  .profile-panel__heading,
  .profile-setting-row,
  .connector-row {
    padding-right: 18px;
    padding-left: 18px;
  }

  .profile-setting-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .profile-setting-row__label {
    min-width: 0;
  }

  .profile-setting-row__input,
  .profile-setting-row__value {
    width: 100%;
    margin-left: 30px;
    text-align: left;
  }

  .profile-setting-row__input :deep(.t-input) {
    text-align: left;
  }

  .connector-row {
    flex-wrap: wrap;
  }

  .connector-row__body {
    min-width: calc(100% - 52px);
  }

  .connector-row > .t-button {
    margin-left: 52px;
  }

  .profile-actions {
    flex-wrap: wrap;
  }
}
</style>
