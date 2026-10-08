<template>
  <section ref="pageRef" class="agent-chat-page">
    <div ref="scrollRef" class="agent-chat-scroll" @scroll.passive="handleChatScroll">
      <div v-if="restoringConversation" class="agent-welcome">
        <h1>正在加载会话</h1>
        <p>请稍候</p>
      </div>

      <div v-else-if="!messages.length" class="agent-welcome">
        <h1>开始新的对话</h1>
        <p>输入指令，Agent 会在后台规划、审批并执行</p>
      </div>

      <div v-else ref="transcriptRef" class="agent-transcript">
        <div v-for="message in messages" :key="message.id" :class="['agent-message', `agent-message--${message.role}`]">
          <AgentUserMessage
            v-if="message.role === 'user'"
            :text="message.text || ''"
            :attachments="message.attachments || []"
            @copy="copyText(message.text || '', '问题已复制')"
            @edit="editQuestion(message.text || '')"
          />
          <article v-else class="agent-response">
            <header class="agent-response__header">
              <span class="agent-response__mark"><AgentMark :active="isAgentActive(message.status)" /></span>
              <div class="agent-response__identity">
                <strong>Agent</strong>
                <AgentStatusIndicator
                  :status="message.status"
                  :text="message.statusText || statusLabel(message.status)"
                />
              </div>
            </header>
            <div class="agent-answer">

              <div
                v-for="(attempt, index) in message.interruptedAnswers || []"
                :key="`${message.id}-interrupted-${index}`"
                class="agent-answer__interrupted"
              >
                <span class="agent-answer__interrupted-label">{{ attempt.reason }}</span>
                <AgentAnswerContent :content="attempt.text" />
              </div>

              <AgentAnswerContent v-if="primaryAnswer(message)" :content="primaryAnswer(message)" />
              <div v-else-if="showsEmptyKnowledgeResult(message)" class="agent-empty-result">
                <strong>没有找到满足条件的内容。</strong>
              </div>

            <div
              v-if="message.reportResult"
              class="agent-report"
              :class="`agent-report--${message.reportResult.status}`"
            >
              <div class="agent-report__head">
                <t-icon :name="message.reportResult.status === 'ready' ? 'file-word' : 'info-circle'" />
                <strong>{{ reportHeadline(message.reportResult) }}</strong>
              </div>

              <template v-if="message.reportResult.status === 'ready'">
                <div class="agent-report__meta">
                  <span v-if="message.reportResult.period">{{ message.reportResult.period }}</span>
                  <span v-if="message.reportResult.file_name">{{ message.reportResult.file_name }}</span>
                  <span v-if="reportRetentionLabel(message.reportResult)">
                    {{ reportRetentionLabel(message.reportResult) }}
                  </span>
                </div>
                <div class="agent-report__actions">
                  <t-button size="small" theme="primary" @click="openReportPreview(message.reportResult, message.reportResult.file_name)">
                    <template #icon><t-icon name="browse" /></template>预览
                  </t-button>
                  <t-button
                    size="small"
                    variant="outline"
                    :loading="reportBusy[message.id] === 'download'"
                    @click="downloadReportFile(message)"
                  >
                    <template #icon><t-icon name="download" /></template>下载
                  </t-button>
                  <t-button
                    size="small"
                    variant="outline"
                    :loading="reportBusy[message.id] === 'archive'"
                    :disabled="reportArchived[message.reportResult.attachment_id]"
                    @click="archiveReport(message)"
                  >
                    <template #icon><t-icon name="folder-add" /></template>
                    {{ reportArchived[message.reportResult.attachment_id] ? '已存入知识库' : '存入知识库' }}
                  </t-button>
                </div>
              </template>

              <template v-else-if="message.reportResult.status === 'needs_selection'">
                <ul class="agent-report__candidates">
                  <li v-for="candidate in message.reportResult.candidates" :key="candidate.attachment_id">
                    <span>{{ candidate.file_name }}</span>
                    <span class="agent-report__candidate-actions">
                      <t-button
                        v-if="candidate.preview_attachment_id"
                        size="small"
                        variant="text"
                        @click="openReportPreview({ attachment_id: candidate.preview_attachment_id }, candidate.file_name)"
                      >预览</t-button>
                      <t-button size="small" variant="outline" theme="primary" @click="chooseReportTemplate(message.reportResult, candidate)">
                        选择
                      </t-button>
                    </span>
                  </li>
                </ul>
              </template>

              <template v-else-if="message.reportResult.status === 'needs_person_choice'">
                <ul class="agent-report__candidates">
                  <li v-for="person in message.reportResult.candidates" :key="person.person_key || person.display_name">
                    <span>{{ person.display_name || person.person_key }}</span>
                  </li>
                </ul>
              </template>
            </div>

              <AgentSourceList
                v-if="visibleSourceItems(message).length"
                :sources="visibleSourceItems(message)"
                :expanded="isSourcesExpanded(message)"
                @toggle="toggleSources(message)"
                @open="openSourceItem"
                @jump="jumpToSourceItem"
              />

              <AgentStepsTimeline
                v-if="message.steps.length"
                :steps="message.steps"
                :open="isTraceOpen(message)"
                @toggle="toggleTrace(message)"
              />

            <div v-if="message.todo" class="agent-todo">
              <div class="agent-todo__title"><t-icon name="task-checked" />待办已创建</div>
              <strong>{{ message.todo.title }}</strong>
              <span>{{ message.todo.due_at || message.todo.due_expression || '未设置截止时间' }}</span>
            </div>

            <div v-if="message.formResult" class="agent-form-result">
              <div class="agent-form-result__title">
                <t-icon name="check-circle" />
                {{ message.formResult.verified ? '已写入表格' : '已提交' }}
              </div>
              <strong>{{ message.formResult.range }}</strong>
              <span v-if="!message.formResult.verified">结果无法自动确认，请自行核对</span>
              <span v-if="formUndone[message.id]" class="agent-form-result__undone">已撤销，已恢复写入前的内容</span>
              <button
                v-if="formUndo[message.id] && !formUndone[message.id]"
                type="button"
                class="agent-button agent-form-result__undo"
                :disabled="undoBusy[message.id]"
                @click="undoForm(message)"
              >撤销</button>
            </div>

            <div
              v-if="message.approval"
              class="agent-approval"
              :class="{ 'agent-approval--form': message.approval.capability === 'form.apply' }"
            >
              <div class="agent-approval__header">
                <span><t-icon name="lock-on" />需要确认</span>
                <small>{{ message.approval.capability }}</small>
              </div>
              <template v-if="message.approval.capability === 'todo.create'">
                <label>
                  <span>标题</span>
                  <input v-model="approvalEditors[message.id].title" maxlength="200" />
                </label>
                <label>
                  <span>截止日期</span>
                  <input v-model="approvalEditors[message.id].dueDate" type="date" />
                </label>
              </template>
              <template v-else-if="message.approval.capability === 'form.apply'">
                <div class="agent-form">
                  <div class="agent-form__head">
                    <div class="agent-form__heading">
                      <p class="agent-form__title">
                        {{ formDraft(message).title || '链接表单' }}
                      </p>
                      <p class="agent-form__summary">
                        {{ formSummary(message).total }} 字段 ·
                        {{ formSummary(message).filled }} 已填
                        <span v-if="formSummary(message).empty"> · {{ formSummary(message).empty }} 待补</span>
                        <span v-if="formDraft(message).target_cell"> · 写入 {{ formDraft(message).target_cell }}</span>
                      </p>
                    </div>
                    <span class="agent-form__percent">{{ formCompletion(message) }}%</span>
                  </div>
                  <div class="agent-form__progress" role="presentation">
                    <span :style="{ width: `${formCompletion(message)}%` }"></span>
                  </div>
                  <p v-if="formDraft(message).missing?.length" class="agent-form__warning">
                    缺少：{{ formDraft(message).missing.join('、') }}
                  </p>
                  <p v-if="formHasReview(message)" class="agent-form__hint">
                    带「请核对」的字段取自共享来源，可能不是该主体的信息，请核对后再确认
                  </p>
                  <p v-else-if="formHasRetrieved(message)" class="agent-form__hint">
                    部分字段来自知识库检索，请核对后再确认
                  </p>
                  <div class="agent-form__fields">
                    <label v-for="(field, index) in formEditor(message)" :key="`${field.name}-${index}`">
                      <span class="agent-form__label">{{ field.name }}</span>
                      <input v-model="field.value" :disabled="message.submitting" />
                      <span class="agent-form__meta">
                        <small
                          class="agent-form__source"
                          :class="`agent-form__source--${formSourceKind(field)}`"
                        >{{ formSourceLabel(field) }}</small>
                        <em v-if="field.evidence" class="agent-form__evidence">{{ field.evidence }}</em>
                      </span>
                    </label>
                  </div>
                </div>
              </template>
              <pre v-else class="agent-approval__arguments">{{ JSON.stringify(message.approval.arguments, null, 2) }}</pre>
              <div class="agent-approval__actions">
                <button type="button" class="agent-button agent-button--primary" :disabled="message.submitting" @click="confirmApproval(message)">确认</button>
                <button type="button" class="agent-button" :disabled="message.submitting" @click="rejectApproval(message)">拒绝</button>
              </div>
            </div>

            <div v-if="isTakeover(message)" class="agent-takeover">
              <p class="agent-takeover__hint">
                目标页面需要登录后才可编辑。下面是在真实浏览器里的画面，直接点击操作，完成后继续。
              </p>
              <img
                v-if="takeoverFrames[message.id]"
                class="agent-takeover__screen"
                :src="takeoverFrames[message.id]"
                alt="接管浏览器画面"
                @click="takeoverClick(message, $event)"
              />
              <p v-else class="agent-takeover__hint">正在获取浏览器画面…</p>
              <div class="agent-takeover__controls">
                <input
                  v-model="takeoverText[message.id]"
                  :disabled="takeoverBusy[message.id]"
                  placeholder="输入要键入的内容"
                  @keydown.enter.prevent="takeoverSendText(message)"
                />
                <button
                  type="button"
                  class="agent-button"
                  :disabled="takeoverBusy[message.id] || !(takeoverText[message.id] || '').trim()"
                  @click="takeoverSendText(message)"
                >键入</button>
                <button
                  type="button"
                  class="agent-button"
                  :disabled="takeoverBusy[message.id]"
                  @click="takeoverPressEnter(message)"
                >回车</button>
              </div>
              <button
                type="button"
                class="agent-button agent-button--primary"
                :disabled="message.submitting"
                @click="continueAfterTakeover(message)"
              >我已完成登录，继续</button>
            </div>

            <form v-else-if="message.inputRequest" class="agent-input-request" @submit.prevent="submitInput(message)">
              <label>
                <span>需要补充的信息</span>
                <small v-if="message.inputRequest.missing.length">{{ message.inputRequest.missing.join('、') }}</small>
                <input v-model="inputValues[message.id]" :disabled="message.submitting" placeholder="请输入补充内容" />
              </label>
              <button type="submit" class="agent-button agent-button--primary" :disabled="message.submitting || !(inputValues[message.id] || '').trim()">提交</button>
            </form>

            <p v-if="message.error" class="agent-error">{{ message.error }}</p>

            <div v-if="primaryAnswer(message) && !message.error" class="agent-answer__actions" aria-label="回答操作">
              <button type="button" title="复制回答" aria-label="复制回答" @click="copyText(primaryAnswer(message), '回答已复制')"><t-icon name="file-copy" /></button>
            </div>
          </div>
          </article>
        </div>
      </div>
    </div>

    <button
      v-if="hasNewChatContent"
      type="button"
      class="agent-scroll-latest"
      aria-label="回到最新内容"
      @click="jumpToLatest({ smooth: true })"
    >
      <t-icon name="chevron-down" />
      <span>有新内容</span>
    </button>

    <div class="agent-composer-area">
      <div class="agent-composer" :class="{ 'agent-composer--focused': focused }">
        <t-textarea
          ref="textareaRef"
          v-model="question"
          class="agent-textarea"
          :autosize="{ minRows: 3, maxRows: 8 }"
          :disabled="Boolean(activeTaskID)"
          placeholder="输入你的指令..."
          @focus="focused = true"
          @blur="focused = false"
          @keydown="handleTextareaKeydown"
        />
        <div class="agent-composer__controls">
          <div class="agent-composer__left">
            <button
              type="button"
              class="agent-attach"
              :class="{ disabled: Boolean(activeTaskID) }"
              :disabled="Boolean(activeTaskID)"
              aria-label="添加附件"
              @click="openFilePicker"
            >
              <t-icon name="attach" />
            </button>
            <input
              ref="fileInputRef"
              class="agent-file-input"
              type="file"
              accept=".pdf,.docx,.png,.jpg,.jpeg"
              :disabled="Boolean(activeTaskID)"
              @change="handleFileChange"
            />
            <span v-if="selectedFile" class="agent-file-chip">
              <t-icon name="file" />
              <span>{{ selectedFile.name }}</span>
              <button type="button" aria-label="移除附件" @click="clearFile"><t-icon name="close" /></button>
            </span>
            <span v-if="activeTaskID" class="agent-composer__hint">Agent 正在执行</span>
            <span v-else-if="uploading" class="agent-composer__hint">正在上传附件</span>
          </div>
          <button
            v-if="activeTaskID"
            type="button"
            class="agent-stop"
            aria-label="终止任务"
            title="终止任务"
            @click="stopActiveTask"
          >
            <t-icon name="stop-circle-filled" />
          </button>
          <button
            v-else
            type="button"
            class="agent-send"
            :class="{ disabled: uploading || !question.trim() }"
            :disabled="uploading || !question.trim()"
            aria-label="发送"
            @click="sendMessage"
          >
            <t-icon name="arrow-up" />
          </button>
        </div>
      </div>
      <p class="agent-disclaimer">内容由 Agent 执行结果生成，仅供参考</p>
    </div>
  </section>

  <t-dialog
    v-model:visible="sourcePreviewVisible"
    attach="body"
    width="min(92vw, 1720px)"
    :footer="false"
    destroy-on-close
    header="文档预览"
    dialog-class-name="agent-source-preview-dialog"
    placement="center"
  >
    <article v-if="activeSourceFile" class="agent-source-preview">
      <main><InfoAttachmentPreview :file="activeSourceFile" :active="sourcePreviewVisible" /></main>
      <footer v-if="activeSourceFile">
        <span>来自 Agent 检索结果</span>
        <div class="agent-source-preview__actions">
          <t-button
            variant="outline"
            :loading="previewDownloading"
            :disabled="activeSourceFile.contentAccessRequired"
            @click="downloadActiveSourceFile"
          >
            <template #icon><t-icon name="download" /></template>下载
          </t-button>
          <t-button v-if="activeSourceTarget?.conversation_id" variant="outline" @click="openActiveSourceConversation">
            <template #icon><t-icon name="chat" /></template>跳转到对应位置
          </t-button>
        </div>
      </footer>
    </article>
  </t-dialog>

  <t-dialog
    v-model:visible="reportPreviewVisible"
    attach="body"
    width="min(92vw, 1720px)"
    :footer="false"
    destroy-on-close
    :header="reportPreviewTitle || '周报预览'"
    dialog-class-name="agent-source-preview-dialog"
    placement="center"
  >
    <article v-if="reportPreviewFile" class="agent-source-preview">
      <main>
        <InfoAttachmentPreview
          :file="reportPreviewFile"
          :active="reportPreviewVisible"
          :loader="reportPreviewLoader"
        />
      </main>
      <footer>
        <span>Agent 生成的周报（未归档文件保留 24 小时）</span>
        <div class="agent-source-preview__actions">
          <t-button variant="outline" :loading="reportPreviewDownloading" @click="downloadReportPreview">
            <template #icon><t-icon name="download" /></template>下载
          </t-button>
        </div>
      </footer>
    </article>
  </t-dialog>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { createLocalUploadTask, getKnowledgeAttachmentContent, listKnowledgeConversationAttachments, uploadLocalContent } from '@/api/info-knowledge'
import InfoAttachmentPreview from '@/components/InfoAttachmentPreview.vue'
import AgentAnswerContent from '@/components/agent-chat/AgentAnswerContent.vue'
import AgentMark from '@/components/agent-chat/AgentMark.vue'
import AgentSourceList from '@/components/agent-chat/AgentSourceList.vue'
import AgentStatusIndicator from '@/components/agent-chat/AgentStatusIndicator.vue'
import AgentStepsTimeline from '@/components/agent-chat/AgentStepsTimeline.vue'
import AgentUserMessage from '@/components/agent-chat/AgentUserMessage.vue'
import { useChatAutoScroll } from '@/composables/useChatAutoScroll'
import type { InfoFile } from '@/mock'
import { useInfoKnowledgeStore } from '@/stores/infoKnowledge'
import { navigateToKnowledgeSource } from '@/utils/knowledge-source-navigation'
import {
  approveAgentApproval,
  buildScheduleDraft,
  cancelAgentTask,
  composeEditedArguments,
  createAgentTask,
  dayOfISO,
  fetchTakeoverFrame,
  fetchAgentReport,
  getAgentAnswerSnapshot,
  getAgentConversation,
  getAgentPlan,
  getAgentTask,
  getFormUndo,
  listAgentApprovals,
  listAgentObservations,
  rejectAgentApproval,
  sendAgentTakeoverInput,
  undoFormWrite,
  streamAgentTaskEvents,
  submitAgentTaskInput,
  uploadAgentAttachment,
  type AgentApproval,
  type AgentConversationDetail,
  type AgentConversationMessage,
  type AgentPlan,
  type AgentTaskEvent,
  type AgentObservation,
} from '@/api/info-agent'

type ChatStatus = string
type Citation = {
  evidence_id: string
  quote?: string
  title?: string
  url?: string
  source?: string
  type?: string
  resource_id?: string
  resource_type?: string
  conversation_id?: string | number | null
  conversation_name?: string
  message_id?: string | number | null
  attachment_id?: string | number | null
  platform?: string
  sender_name?: string
  sent_at?: string
  position?: Record<string, any> | null
}
type TodoResult = { todo_id: string; title?: string; due_at?: string | null; due_expression?: string | null }
type ReportTemplateCandidate = {
  attachment_id?: string
  file_name?: string
  origin?: string
  preview_attachment_id?: string
  /** Person candidates reuse the same list shape when the name is ambiguous. */
  person_key?: string
  display_name?: string
}
/** A report.weekly result: the artifact handle plus the traceability list. */
type ReportResult = {
  status: string
  summary?: string
  title?: string
  subject_name?: string
  period?: string
  file_name?: string
  /** Always present: the capture step normalises a missing id to "". */
  attachment_id: string
  size_bytes?: number
  expires_at?: string
  template_origin?: string
  candidates?: ReportTemplateCandidate[]
}
type Step = { id: string; label: string; status: string; stages?: string[] }
type SourceInfo = {
  resource_id: string
  resource_type: string
  knowledge_item_id?: string | null
  title?: string | null
  file_name?: string | null
  sender_id?: string | null
  sender_name?: string | null
  conversation_id?: string | null
  conversation_name?: string | null
  conversation_platform?: string | null
  sent_at?: string | null
  preview?: string | null
  score?: number
}
type ContentChunk = { chunk_id: string; text: string; score?: number; position?: Record<string, any> | null }
type ContentResult = {
  resource_id: string
  resource_type: string
  title?: string | null
  sender_name?: string | null
  conversation_name?: string | null
  conversation_id?: string | null
  sent_at?: string | null
  chunks: ContentChunk[]
  best_score?: number
}
type SourceListBlock = {
  type: 'source_list'
  summary: string
  items: SourceInfo[]
  resource_ids: string[]
  metadata_coverage: 'complete' | 'partial'
}
type ContentResultsBlock = {
  type: 'content_results'
  results: ContentResult[]
  metadata_coverage: 'complete' | 'partial'
}
type AnswerBlock = {
  type: 'answer'
  text: string
  citations: Citation[]
}
type AgentResultBlock = SourceListBlock | ContentResultsBlock | AnswerBlock
type SourceItem = {
  key: string
  title: string
  meta: string
  preview?: string
  kind: 'attachment' | 'message' | 'source'
  resource_id?: string
  conversation_id?: string | number | null
  conversation_name?: string
  message_id?: string | number | null
  platform?: string
}
type AgentMessage = {
  id: string
  role: 'user' | 'agent'
  text?: string
  /** Files attached to a user turn, kept locally so the sent bubble can show them. */
  attachments?: { id: string; name: string }[]
  taskId?: string
  status?: ChatStatus
  statusText?: string
  steps: Step[]
  blocks: AgentResultBlock[]
  answer?: string
  /** Throttled display buffer while an answer is streaming. */
  streamText?: string
  answerId?: string
  answerStepId?: string
  answerAttempt?: number
  answerNextSeq?: number
  answerStreaming?: boolean
  answerStreamError?: string
  /** Earlier attempts preserved as "generation interrupted" history. */
  interruptedAnswers?: { text: string; reason: string }[]
  citations: Citation[]
  error?: string
  todo?: TodoResult
  /** Receipt for a form.preview / form.apply step, shown after a write. */
  formResult?: { summary: string; range?: string; verified?: boolean }
  /** Weekly-report result: file handle, period, sources or template choices. */
  reportResult?: ReportResult
  approval?: AgentApproval
  inputRequest?: { missing: string[] }
  submitting?: boolean
  lastEventId: number
}

const question = ref('')
const focused = ref(false)
const uploading = ref(false)
const selectedFile = ref<File | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const messages = ref<AgentMessage[]>([])
const scrollRef = ref<HTMLElement | null>(null)
const transcriptRef = ref<HTMLElement | null>(null)
const pageRef = ref<HTMLElement | null>(null)
const textareaRef = ref<{ focus?: () => void } | null>(null)
const activeTaskID = ref('')
const conversationId = ref('')
const restoringConversation = ref(false)
const approvalEditors = reactive<Record<string, { title: string; dueDate: string }>>({})
/**
 * The editable field list of a form.apply draft, keyed by message id. The
 * owner's edits are what the confirm call sends back, so the values written
 * are the ones on screen, not the ones the Agent proposed.
 */
/** One editable cell of a form draft, plus where a retrieved value came from. */
type FormEditorField = {
  name: string
  value: string
  source: string
  subject: string
  tier: string
  evidence: string
}
const formEditors = reactive<Record<string, FormEditorField[]>>({})
const inputValues = reactive<Record<string, string>>({})
const expandedTraces = reactive<Record<string, boolean>>({})
const expandedSources = reactive<Record<string, boolean>>({})
const {
  hasNewContent: hasNewChatContent,
  bind: bindChatScroll,
  handleScroll: handleChatScroll,
  jumpToLatest,
  reset: resetChatScroll,
  scheduleFollow: scheduleChatFollow,
  dispose: disposeChatScroll,
} = useChatAutoScroll()
let activeController: AbortController | null = null
let conversationLoadSequence = 0
const router = useRouter()
const route = useRoute()
watch(
  [scrollRef, transcriptRef],
  () => bindChatScroll(scrollRef.value, transcriptRef.value),
  { immediate: true, flush: 'post' },
)
const sourcePreviewVisible = ref(false)
const previewDownloading = ref(false)
const activeSourceFile = ref<InfoFile | null>(null)
/** Weekly-report preview: its own dialog because the bytes come from the Agent. */
const reportPreviewVisible = ref(false)
const reportPreviewDownloading = ref(false)
const reportPreviewFile = ref<InfoFile | null>(null)
const reportPreviewTitle = ref('')
const reportPreviewAttachmentID = ref('')
const reportBusy = reactive<Record<string, string>>({})
const reportArchived = reactive<Record<string, boolean>>({})
const activeSourceTarget = ref<SourceInfo | Citation | null>(null)

function newMessageID(): string {
  return globalThis.crypto?.randomUUID?.() || `message-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function statusLabel(status?: string): string {
  return ({
    received: '任务已接收',
    planning: '正在规划',
    ready: '计划已生成',
    executing: '正在执行',
    waiting_approval: '等待确认',
    waiting_input: '等待补充信息',
    succeeded: '任务已完成',
    failed: '任务失败',
    unknown: '外部结果未知',
    cancelled: '任务已取消',
  } as Record<string, string>)[status || ''] || '正在处理'
}

function isAgentActive(status?: string): boolean {
  return ['received', 'planning', 'ready', 'executing', 'waiting_approval', 'waiting_input'].includes(status || '')
}

function statusFromHistoryMessage(status: string): string {
  return ({
    completed: 'succeeded',
    pending: 'executing',
    streaming: 'executing',
    failed: 'failed',
    cancelled: 'cancelled',
  } as Record<string, string>)[status] || status || 'executing'
}

function notifyConversationChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('agent-conversation-updated'))
  }
}

function approvalArguments(event: AgentTaskEvent): Record<string, any> {
  return event.payload?.arguments && typeof event.payload.arguments === 'object'
    ? event.payload.arguments
    : {}
}

function eventTaskID(event: AgentTaskEvent, fallback: string): string {
  return String(event.task_id || event.payload?.task_id || fallback)
}

function rememberEvent(message: AgentMessage, event: AgentTaskEvent): void {
  message.lastEventId = Math.max(message.lastEventId, Number(event.sequence || 0))
}

async function prepareApproval(message: AgentMessage, event: AgentTaskEvent): Promise<void> {
  stopTakeover(message)
  const args = approvalArguments(event)
  const timezone = String(args.timezone || 'Asia/Shanghai')
  const approval: AgentApproval = {
    approval_id: String(event.payload?.approval_id || ''),
    task_id: eventTaskID(event, message.taskId || ''),
    plan_id: String(event.payload?.plan_id || ''),
    step_id: String(event.payload?.step_id || ''),
    capability: String(event.payload?.capability || ''),
    arguments: args,
    version: Number(event.payload?.version || 1),
    status: 'waiting_approval',
  }
  message.approval = approval
  approvalEditors[message.id] = {
    title: String(args.title || approval.capability || '待确认操作'),
    dueDate: typeof args.due_at === 'string' ? dayOfISO(args.due_at, timezone) : '',
  }
  if (approval.capability === 'form.apply') {
    formEditors[message.id] = formFieldsFrom(args.draft)
  }
  message.status = 'waiting_approval'
  message.statusText = '等待确认'
}

function addOrUpdateStep(message: AgentMessage, event: AgentTaskEvent): void {
  const stepID = String(event.payload?.step_id || event.payload?.step?.step_id || '')
  if (!stepID) return
  const label = String(event.payload?.capability || event.payload?.step?.capability || stepID)
  // A capability may report what it did in the owner's words; those lines hang
  // under the step so its internal work is not one opaque row.
  const rawStages = event.payload?.result_preview?.stages
  const stages = Array.isArray(rawStages) ? rawStages.map((item) => String(item)) : []
  const current = message.steps.find((step) => step.id === stepID)
  const status = event.event_type === 'step.succeeded' ? 'succeeded' : event.event_type === 'step.failed' ? 'failed' : 'running'
  if (current) {
    current.label = label
    current.status = status
    if (stages.length) current.stages = stages
  } else {
    message.steps.push({ id: stepID, label, status, stages })
  }
}

function handleTaskEvent(message: AgentMessage, event: AgentTaskEvent): boolean | void {
  rememberEvent(message, event)
  const payload = event.payload || {}
  switch (event.event_type) {
    case 'task.accepted':
      message.status = 'received'
      message.statusText = '任务已接收'
      break
    case 'task.planning':
      message.status = 'planning'
      message.statusText = '正在理解与规划'
      break
    case 'task.understanding':
      message.status = 'planning'
      message.statusText = payload.decision_source === 'laya' ? '意图识别完成' : payload.decision_source === 'llm' ? '意图识别完成（LLM）' : '意图识别完成'
      break
    case 'plan.created':
      message.status = 'ready'
      message.statusText = `计划已生成（${Array.isArray(payload.steps) ? payload.steps.length : 0} 步）`
      break
    case 'plan.replanned':
      message.status = 'planning'
      message.statusText = '计划已调整'
      break
    case 'planner.decision':
      message.statusText = payload.action ? `规划决策：${payload.action}` : '规划决策已更新'
      break
    case 'step.started':
    case 'step.succeeded':
    case 'step.failed':
      addOrUpdateStep(message, event)
      if (event.event_type === 'step.succeeded') {
        const preview = payload.result_preview as Record<string, unknown> | undefined
        const writtenRange = typeof preview?.written_range === 'string' ? preview.written_range : ''
        // A preview step is only a progress line; a write produces the receipt.
        if (writtenRange && typeof preview?.summary === 'string') {
          message.formResult = {
            summary: preview.summary,
            range: writtenRange,
            verified: Boolean(preview?.verified),
          }
          void refreshFormUndo(message)
        }
        if (preview?.block_type === 'report') {
          message.reportResult = reportResultFromPayload(preview)
          const reportCitations = citationsFromPayload(preview)
          if (reportCitations.length) message.citations = reportCitations
        }
      }
      message.status = event.event_type === 'step.failed' ? 'executing' : 'executing'
      message.statusText = event.event_type === 'step.started'
        ? stepStartText(String(payload.capability || ''))
        : event.event_type === 'step.succeeded'
          ? previewSummary(payload.result_preview) || stepSucceededText(String(payload.capability || ''))
          : `步骤失败：${displayStepLabel(String(payload.capability || '能力'))}`
      break
    case 'task.waiting_approval':
      void prepareApproval(message, event)
      return false
    case 'task.waiting_input':
      message.status = 'waiting_input'
      message.statusText = '需要补充信息'
      message.inputRequest = { missing: Array.isArray(payload.missing_information) ? payload.missing_information.map(String) : [] }
      startTakeover(message)
      return false
    case 'task.preview_confirmed':
      message.todo = {
        todo_id: String(payload.todo_id || ''),
        title: typeof payload.title === 'string' ? payload.title : undefined,
        due_at: typeof payload.due_at === 'string' ? payload.due_at : null,
        due_expression: typeof payload.due_expression === 'string' ? payload.due_expression : null,
      }
      break
    case 'approval.rejected':
      message.status = 'failed'
      message.statusText = '审批已拒绝'
      message.error = '审批已拒绝，任务不会执行'
      return false
    case 'answer.started': {
      const nextId = String(payload.answer_id || '')
      if (
        nextId
        && message.answerId
        && nextId !== message.answerId
        && (message.streamText || message.answer || '').trim()
      ) {
        flushStreamText(message)
        message.interruptedAnswers = [
          ...(message.interruptedAnswers || []),
          {
            text: message.streamText || message.answer || '',
            reason: message.answerStreamError === 'cancelled' ? '已取消' : '生成中断',
          },
        ]
        message.streamText = ''
        message.answer = ''
      }
      if (nextId) message.answerId = nextId
      message.answerStepId = String(payload.step_id || '')
      message.answerAttempt = Number(payload.attempt || 0)
      message.answerNextSeq = Math.max(1, Number(payload.next_seq || 1))
      message.answerStreaming = true
      message.answerStreamError = ''
      message.status = 'executing'
      message.statusText = '正在生成回答'
      break
    }
    case 'answer.delta': {
      if (!ANSWER_STREAMING_ENABLED) break
      const seq = Number(payload.seq || 0)
      const next = Math.max(1, Number(message.answerNextSeq || 1))
      if (seq < next) break
      if (seq > next) {
        void recoverAnswerSnapshot(message)
        break
      }
      appendStreamText(message, String(payload.delta || ''))
      message.answerNextSeq = seq + 1
      message.answerStreaming = true
      break
    }
    case 'answer.completed': {
      flushStreamText(message)
      if (typeof payload.answer === 'string' && payload.answer) {
        message.answer = payload.answer
        message.streamText = payload.answer
      }
      if (Array.isArray(payload.citations)) {
        message.citations = payload.citations.map(mapCitation)
      }
      message.answerStreaming = false
      message.answerNextSeq = Number(payload.final_seq || 0) + 1
      break
    }
    case 'task.completed':
      flushStreamText(message)
      message.answerStreaming = false
      stopTakeover(message)
      message.status = 'succeeded'
      message.statusText = '任务已完成'
      if (typeof payload.answer === 'string') message.answer = payload.answer
      if (Array.isArray(payload.citations)) message.citations = payload.citations.map(mapCitation)
      message.blocks = blocksFromObservations([], message.answer, message.citations)
      void hydrateTerminal(message)
      return false
    case 'task.failed':
      message.answerStreaming = false
      message.answerStreamError = 'interrupted'
      stopTakeover(message)
      message.status = payload.task_status === 'unknown' ? 'unknown' : 'failed'
      message.statusText = payload.task_status === 'unknown' ? '外部结果未知' : '任务失败'
      message.error = String(payload.error?.message || '任务执行失败')
      return false
    case 'task.cancelled':
      message.answerStreaming = false
      message.answerStreamError = 'cancelled'
      stopTakeover(message)
      message.status = 'cancelled'
      message.statusText = '任务已取消'
      return false
  }
}

function mapCitation(value: any): Citation {
  return {
    evidence_id: String(value?.evidence_id || value?.source_id || ''),
    quote: typeof value?.quote === 'string' ? value.quote : undefined,
    title: typeof value?.title === 'string' ? value.title : undefined,
    url: typeof value?.url === 'string' ? value.url : undefined,
    source: typeof value?.source === 'string' ? value.source : undefined,
    type: typeof value?.type === 'string' ? value.type : undefined,
    resource_id: value?.resource_id == null ? undefined : String(value.resource_id),
    resource_type: typeof value?.resource_type === 'string' ? value.resource_type : undefined,
    conversation_id: value?.conversation_id ?? value?.source_conversation_id ?? null,
    conversation_name: typeof value?.conversation_name === 'string' ? value.conversation_name : undefined,
    message_id: value?.message_id ?? value?.source_message_id ?? null,
    attachment_id: value?.attachment_id ?? null,
    platform: typeof value?.platform === 'string'
      ? value.platform
      : typeof value?.source_platform === 'string'
        ? value.source_platform
        : typeof value?.conversation_platform === 'string'
          ? value.conversation_platform
          : undefined,
    sender_name: typeof value?.sender_name === 'string' ? value.sender_name : undefined,
    sent_at: typeof value?.sent_at === 'string' ? value.sent_at : undefined,
    position: value?.position && typeof value.position === 'object' ? value.position : null,
  }
}

/** The weekly-report card, rebuilt from the live step preview or a stored observation. */
function reportResultFromPayload(preview: Record<string, any>): ReportResult {
  return {
    status: String(preview?.status || ''),
    summary: typeof preview?.summary === 'string' ? preview.summary : '',
    title: typeof preview?.title === 'string' ? preview.title : '',
    subject_name: typeof preview?.subject_name === 'string' ? preview.subject_name : '',
    period: typeof preview?.period === 'string' ? preview.period : '',
    file_name: typeof preview?.file_name === 'string' ? preview.file_name : '',
    attachment_id: typeof preview?.attachment_id === 'string' ? preview.attachment_id : '',
    size_bytes: Number(preview?.size_bytes || 0),
    expires_at: typeof preview?.expires_at === 'string' ? preview.expires_at : '',
    template_origin: typeof preview?.template_origin === 'string' ? preview.template_origin : '',
    candidates: Array.isArray(preview?.candidates)
      ? (preview.candidates as ReportTemplateCandidate[])
      : [],
  }
}

/** A report's sources use the answer citation shape, so they share the same list. */
function citationsFromPayload(preview: Record<string, any>): Citation[] {
  if (!Array.isArray(preview?.citations)) return []
  return preview.citations.map((value: any) => mapCitation(value))
}

function previewSummary(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const summary = (value as Record<string, unknown>).summary
  return typeof summary === 'string' ? summary.trim() : ''
}

const ANSWER_STREAMING_ENABLED = String(
  (import.meta as ImportMeta & { env?: Record<string, string> }).env
    ?.VITE_AGENT_ANSWER_STREAMING_ENABLED || 'false',
).toLowerCase() === 'true'

/** Deltas are buffered and flushed on a timer so Markdown is not re-rendered
 * once per network chunk. */
const streamBuffers = new Map<string, string>()
const streamTimers = new Map<string, number>()

function appendStreamText(message: AgentMessage, delta: string): void {
  if (!delta) return
  const buffered = (streamBuffers.get(message.id) ?? message.streamText ?? '') + delta
  streamBuffers.set(message.id, buffered)
  if (streamTimers.has(message.id)) return
  const timer = window.setTimeout(() => {
    streamTimers.delete(message.id)
    const value = streamBuffers.get(message.id)
    if (value !== undefined) message.streamText = value
  }, 40)
  streamTimers.set(message.id, timer)
}

function flushStreamText(message: AgentMessage): void {
  const timer = streamTimers.get(message.id)
  if (timer) window.clearTimeout(timer)
  streamTimers.delete(message.id)
  const value = streamBuffers.get(message.id)
  streamBuffers.delete(message.id)
  if (value !== undefined) message.streamText = value
}

async function recoverAnswerSnapshot(message: AgentMessage): Promise<void> {
  if (!message.taskId || !message.answerId || message.answerStreamError === 'recovering') return
  message.answerStreamError = 'recovering'
  try {
    const snapshot = await getAgentAnswerSnapshot(message.taskId, message.answerId)
    message.streamText = snapshot.text || message.streamText || ''
    message.answerNextSeq = Number(snapshot.next_seq || 0) + 1
    message.answerStreamError = ''
    activeController?.abort()
    void watchTask(message, message.lastEventId)
  } catch {
    message.answerStreamError = '回答恢复失败'
  }
}

function primaryAnswer(message: AgentMessage): string {
  if (message.answerStreaming && typeof message.streamText === 'string') {
    const streaming = message.streamText.trim()
    if (streaming) return streaming
  }
  const block = message.blocks.find((item) => item.type === 'answer')
  const text = (message.answer || (block?.type === 'answer' ? block.text : '') || '').trim()
  if (!text || text === '没有找到满足条件的内容。') return ''
  return text
}

function showsEmptyKnowledgeResult(message: AgentMessage): boolean {
  if (message.status !== 'succeeded') return false
  // A weekly-report card is its own result: it carries the file and the
  // sources, so the "nothing found" placeholder must not sit above it.
  if (primaryAnswer(message) || message.todo || message.formResult || message.approval || message.inputRequest || message.reportResult) return false
  return !visibleSourceItems(message).length
}

function visibleSourceItems(message: AgentMessage): SourceItem[] {
  const items = new Map<string, SourceItem>()
  if (message.citations.length) {
    for (const citation of message.citations) {
      const item = sourceItemFromCitation(citation)
      if (item) items.set(item.key, item)
    }
    return [...items.values()]
  }
  for (const block of message.blocks) {
    if (block.type !== 'source_list' || !block.items.length) continue
    for (const source of block.items) {
      const item = sourceItemFromSource(source)
      items.set(item.key, item)
    }
  }
  return [...items.values()]
}

function sourceItemFromCitation(citation: Citation): SourceItem | null {
  const key = citationKey(citation)
  const kind: SourceItem['kind'] = citation.resource_type === 'attachment' ? 'attachment' : citation.conversation_id ? 'message' : 'source'
  return {
    key,
    kind,
    title: displayTitleForCitation(citation),
    meta: [citation.sender_name, citation.conversation_name, formatAgentMoment(citation.sent_at)].filter(Boolean).join(' · '),
    preview: truncateText(citation.quote, 120),
    resource_id: citation.resource_id || (citation.attachment_id != null ? String(citation.attachment_id) : undefined),
    conversation_id: citation.conversation_id,
    conversation_name: citation.conversation_name,
    message_id: citation.message_id,
    platform: citation.platform,
  }
}

function sourceItemFromSource(source: SourceInfo): SourceItem {
  const kind: SourceItem['kind'] = source.resource_type === 'attachment' ? 'attachment' : 'message'
  return {
    key: sourceKey(source),
    kind,
    title: displayTitleForSource(source),
    meta: sourceMeta(source),
    preview: truncateText(source.preview, 120),
    resource_id: source.resource_id,
    message_id: source.resource_type === 'attachment' ? undefined : source.resource_id,
    conversation_id: source.conversation_id,
    conversation_name: source.conversation_name || undefined,
    platform: source.conversation_platform || undefined,
  }
}

function displayTitleForCitation(citation: Citation): string {
  if (citation.resource_type === 'attachment') return citation.title || citation.source || '附件'
  if (citation.title && !looksLikeInternalID(citation.title)) return citation.title
  if (citation.quote) return truncateText(citation.quote, 42) || '知识库消息'
  return citation.conversation_name ? `${citation.conversation_name}消息` : '知识库消息'
}

function displayTitleForSource(source: SourceInfo): string {
  if (source.resource_type === 'attachment') return source.file_name || source.title || source.preview || '附件'
  if (source.title && !looksLikeInternalID(source.title)) return source.title
  if (source.preview) return truncateText(source.preview, 42) || '知识库消息'
  return source.conversation_name ? `${source.conversation_name}消息` : '知识库消息'
}

function looksLikeInternalID(value: string): boolean {
  const text = value.trim()
  return /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(text) || /^[0-9a-f]{20,}$/i.test(text)
}

function truncateText(value?: string | null, limit = 80): string | undefined {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (!text) return undefined
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

function isTraceOpen(message: AgentMessage): boolean {
  return expandedTraces[message.id] === true
}

function toggleTrace(message: AgentMessage): void {
  expandedTraces[message.id] = !isTraceOpen(message)
}

function isSourcesExpanded(message: AgentMessage): boolean {
  return expandedSources[message.id] === true
}

function toggleSources(message: AgentMessage): void {
  expandedSources[message.id] = !isSourcesExpanded(message)
}

function displayStepLabel(label: string): string {
  return ({
    'form.preview': '阅读表单并预填',
    'form.apply': '写入表格',
    'knowledge.search_sources': '检索本地知识',
    'knowledge.search_content': '查找相关内容',
    'knowledge.answer': '整理回答',
    'web.fetch': '读取网页',
    'web.extract': '提取网页内容',
    'answer.compose': '整理回答',
    'todo.create': '创建待办',
  } as Record<string, string>)[label] || label
}

function stepStartText(capability: string): string {
  return ({
    'form.preview': '正在打开并阅读表单',
    'form.apply': '正在写入表格',
    'knowledge.search_sources': '正在检索本地知识',
    'knowledge.search_content': '正在查找相关内容',
    'knowledge.answer': '正在整理回答',
    'web.fetch': '正在读取网页',
    'web.extract': '正在提取网页内容',
    'answer.compose': '正在整理回答',
    'todo.create': '正在创建待办',
  } as Record<string, string>)[capability] || `正在执行 ${displayStepLabel(capability || '能力')}`
}

function stepSucceededText(capability: string): string {
  return ({
    'form.preview': '表单已预填',
    'form.apply': '写入完成',
    'knowledge.search_sources': '本地知识检索完成',
    'knowledge.search_content': '相关内容查找完成',
    'knowledge.answer': '回答已生成',
    'web.fetch': '网页读取完成',
    'web.extract': '网页内容提取完成',
    'answer.compose': '回答已生成',
    'todo.create': '待办已创建',
  } as Record<string, string>)[capability] || `${displayStepLabel(capability || '步骤')}完成`
}

function stepsFromPlan(plan: AgentPlan | null): Step[] {
  if (!plan?.steps?.length) return []
  return plan.steps.map((step) => ({
    id: step.step_id,
    label: step.capability,
    status: step.status || 'pending',
  }))
}

function citationKey(citation: Citation): string {
  return citation.evidence_id || citation.url || citation.title || citation.quote || 'citation'
}

function sourceKey(source: SourceInfo): string {
  return `${source.resource_type}:${source.resource_id}`
}

function sourceLabel(source: SourceInfo): string {
  return source.title || source.file_name || source.preview || source.resource_id
}

function sourceMeta(source: SourceInfo): string {
  return [source.sender_name, source.conversation_name, formatAgentMoment(source.sent_at)].filter(Boolean).join(' · ')
}

function resultMeta(result: ContentResult): string {
  return [result.sender_name, result.conversation_name, formatAgentMoment(result.sent_at)].filter(Boolean).join(' · ')
}

function citationMeta(citation: Citation): string {
  return [citation.sender_name, citation.conversation_name, formatAgentMoment(citation.sent_at)].filter(Boolean).join(' · ')
}

function formatAgentMoment(value?: string | null): string {
  const raw = String(value || '').trim()
  if (!raw) return ''
  const moment = new Date(raw)
  if (Number.isNaN(moment.getTime())) return raw
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Shanghai',
  }).format(moment)
}

function blocksFromObservations(
  observations: AgentObservation[],
  fallbackAnswer?: string,
  fallbackCitations?: Citation[],
): AgentResultBlock[] {
  const blocks: AgentResultBlock[] = []
  for (const observation of observations) {
    if (observation.status !== 'succeeded') continue
    const output = observation.output || {}
    if (observation.capability === 'knowledge.search_sources' && Array.isArray(output.sources)) {
      blocks.push({
        type: 'source_list',
        summary: String(output.summary || ''),
        items: output.sources as SourceInfo[],
        resource_ids: Array.isArray(output.resource_ids) ? output.resource_ids.map(String) : [],
        metadata_coverage: output.metadata_coverage === 'partial' ? 'partial' : 'complete',
      })
    } else if (observation.capability === 'knowledge.search_content' && Array.isArray(output.results)) {
      blocks.push({
        type: 'content_results',
        results: output.results as ContentResult[],
        metadata_coverage: output.metadata_coverage === 'partial' ? 'partial' : 'complete',
      })
    } else if (observation.capability === 'knowledge.answer' && typeof output.answer === 'string') {
      blocks.push({
        type: 'answer',
        text: output.answer,
        citations: Array.isArray(output.citations) ? output.citations.map(mapCitation) : [],
      })
    }
  }
  if (!blocks.some((block) => block.type === 'answer') && fallbackAnswer) {
    blocks.push({
      type: 'answer',
      text: fallbackAnswer,
      citations: fallbackCitations || [],
    })
  }
  return blocks
}

function evidenceForObservations(observations: AgentObservation[]): Map<string, Citation> {
  const found = new Map<string, Citation>()
  for (const observation of observations) {
    const evidence = observation.output?.evidence
    if (!Array.isArray(evidence)) continue
    for (const item of evidence) {
      if (!item || typeof item !== 'object') continue
      const citation = mapCitation(item)
      if (citation.evidence_id) found.set(citation.evidence_id, citation)
    }
  }
  return found
}

function todoFromObservations(observations: AgentObservation[]): TodoResult | undefined {
  for (const observation of observations) {
    const output = observation.output
    if (observation.status === 'succeeded' && output?.todo_id) {
      return {
        todo_id: String(output.todo_id),
        title: typeof output.title === 'string' ? output.title : undefined,
        due_at: typeof output.due_at === 'string' ? output.due_at : null,
        due_expression: typeof output.due_expression === 'string' ? output.due_expression : null,
      }
    }
  }
  return undefined
}

async function openSourceItem(source: SourceItem): Promise<void> {
  if (source.kind === 'attachment' && source.resource_id) {
    await openSource({
      resource_id: source.resource_id,
      resource_type: 'attachment',
      title: source.title,
      file_name: source.title,
      conversation_id: source.conversation_id ? String(source.conversation_id) : null,
    })
    return
  }
  await jumpToSourceItem(source)
}

async function jumpToSourceItem(source: SourceItem): Promise<void> {
  const knowledgeStore = useInfoKnowledgeStore()
  let conversationID = source.conversation_id ? String(source.conversation_id) : ''
  let platform = source.platform
  if (!conversationID && source.conversation_name) {
    if (!knowledgeStore.allChats.length) await knowledgeStore.ensureSources()
    const match = knowledgeStore.allChats.find((item) => item.name === source.conversation_name)
    if (match) {
      conversationID = String(match.id)
      platform = match.source
    }
  }
  if (!conversationID) {
    MessagePlugin.info('该来源暂无可定位的会话位置')
    return
  }
  try {
    await navigateToKnowledgeSource(router, route, {
      platform,
      conversationId: conversationID,
      messageId: source.kind === 'attachment' ? null : source.message_id || source.resource_id,
      attachmentId: source.kind === 'attachment' ? source.resource_id : null,
    })
  } catch {
    MessagePlugin.error('无法定位来源位置，请稍后重试')
  }
}

async function openSource(source: SourceInfo): Promise<void> {
  if (source.resource_type === 'attachment') {
    let name = source.file_name || source.title || '附件'
    let mimeType: string | undefined
    let contentAccessRequired = false
    let fileSizeBytes: number | null = null
    if (source.conversation_id) {
      try {
        const response = await listKnowledgeConversationAttachments(String(source.conversation_id))
        const metadata: any = (response.items || []).find((item: any) =>
          String(item.id || item.attachment_id || '') === String(source.resource_id)
          || String(item.external_attachment_id || '') === String(source.resource_id),
        )
        name = String(metadata?.file_name || metadata?.name || name)
        mimeType = String(metadata?.mime_type || metadata?.mimeType || '') || undefined
        contentAccessRequired = Boolean(metadata?.content_access_required)
        fileSizeBytes = Number(metadata?.size_bytes || 0) || null
      } catch {
        // The content endpoint still provides the definitive error state.
      }
    }
    activeSourceTarget.value = source
    activeSourceFile.value = {
      id: source.resource_id,
      name,
      type: name.includes('.') ? name.split('.').pop() || 'file' : 'file',
      mimeType,
      size: fileSizeBytes ? `${fileSizeBytes} bytes` : '-',
      time: '',
      uploadedAt: '',
      uploader: '',
      content: '',
      contentAccessRequired,
      fileSizeBytes,
      documentStatus: 'completed',
      parseStatus: 'completed',
    }
    sourcePreviewVisible.value = true
    return
  }
  await openSourceConversation(source)
}

async function openContentResult(result: ContentResult): Promise<void> {
  await openSource({
    resource_id: result.resource_id,
    resource_type: result.resource_type,
    title: result.title,
    file_name: result.title,
    conversation_name: result.conversation_name,
    sent_at: result.sent_at,
    conversation_id: result.conversation_id || null,
  })
}

async function openCitation(citation: Citation): Promise<void> {
  if (citation.resource_type === 'attachment') {
    await openContentResult({
      resource_id: String(citation.resource_id || citation.attachment_id || ''),
      resource_type: 'attachment',
      title: citation.title || citation.source,
      sender_name: citation.sender_name,
      conversation_name: citation.conversation_name,
      conversation_id: citation.conversation_id ? String(citation.conversation_id) : null,
      sent_at: citation.sent_at,
      chunks: [],
    })
    return
  }
  if (citation.conversation_id) {
    try {
      await navigateToKnowledgeSource(router, route, {
        platform: citation.platform,
        conversationId: citation.conversation_id,
        messageId: citation.message_id,
      })
    } catch {
      MessagePlugin.error('无法定位来源会话，请稍后重试')
    }
    return
  }
  MessagePlugin.info('该来源暂无可打开的详情')
}

async function openSourceConversation(source: SourceInfo | Citation): Promise<void> {
  if (!source.conversation_id) return
  try {
    await navigateToKnowledgeSource(router, route, {
      platform: platformForSource(source),
      conversationId: source.conversation_id,
      messageId: 'message_id' in source ? source.message_id : null,
      attachmentId: source.resource_type === 'attachment' ? source.resource_id : null,
    })
  } catch {
    MessagePlugin.error('无法定位来源会话，请稍后重试')
  }
}

function platformForSource(source: SourceInfo | Citation): string | undefined {
  return (source as SourceInfo).conversation_platform || (source as Citation).platform
}

async function openActiveSourceConversation(): Promise<void> {
  if (!activeSourceTarget.value) return
  await openSourceConversation(activeSourceTarget.value)
  sourcePreviewVisible.value = false
}

async function downloadActiveSourceFile(): Promise<void> {
  const file = activeSourceFile.value
  if (!file || file.contentAccessRequired || previewDownloading.value) return
  previewDownloading.value = true
  try {
    const blob = await getKnowledgeAttachmentContent(file.id, true)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = file.name || 'attachment'
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    MessagePlugin.error(error instanceof Error ? error.message : '附件下载失败')
  } finally {
    previewDownloading.value = false
  }
}

function reportHeadline(result: ReportResult): string {
  if (result.status === 'ready') {
    return result.title || (result.subject_name ? `${result.subject_name}的周报` : '周报')
  }
  if (result.status === 'needs_selection') return '请选择要使用的模板'
  if (result.status === 'needs_person_choice') return '请确认是哪一位'
  return result.summary || '写周报'
}

function reportRetentionLabel(result: ReportResult): string {
  if (!result.expires_at) return ''
  const expires = new Date(result.expires_at)
  if (Number.isNaN(expires.getTime())) return ''
  return `未归档文件保留至 ${expires.toLocaleString('zh-CN', { hour12: false })}`
}

function reportPreviewLoader(download = false): Promise<Blob> {
  return fetchAgentReport(reportPreviewAttachmentID.value, download)
}

function openReportPreview(target: { attachment_id?: string } | undefined | null, fileName?: string): void {
  const id = String(target?.attachment_id || '')
  if (!id) return
  reportPreviewAttachmentID.value = id
  reportPreviewTitle.value = fileName || '周报预览'
  reportPreviewFile.value = {
    id,
    name: fileName || '周报.docx',
    type: 'docx',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    size: '-',
    time: '',
    uploadedAt: '',
    uploader: '',
    content: '',
    documentStatus: 'completed',
    parseStatus: 'completed',
  }
  reportPreviewVisible.value = true
}

function saveReportBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName || '周报.docx'
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function downloadReportFile(message: AgentMessage): Promise<void> {
  const result = message.reportResult
  if (!result?.attachment_id || reportBusy[message.id]) return
  reportBusy[message.id] = 'download'
  try {
    saveReportBlob(
      await fetchAgentReport(result.attachment_id, true),
      result.file_name || '周报.docx',
    )
  } catch (error) {
    MessagePlugin.error(error instanceof Error ? error.message : '周报下载失败')
  } finally {
    delete reportBusy[message.id]
  }
}

async function downloadReportPreview(): Promise<void> {
  if (!reportPreviewAttachmentID.value || reportPreviewDownloading.value) return
  reportPreviewDownloading.value = true
  try {
    saveReportBlob(
      await fetchAgentReport(reportPreviewAttachmentID.value, true),
      reportPreviewFile.value?.name || '周报.docx',
    )
  } catch (error) {
    MessagePlugin.error(error instanceof Error ? error.message : '周报下载失败')
  } finally {
    reportPreviewDownloading.value = false
  }
}

/**
 * Archiving is the user's own write into their personal local library, so it
 * runs through the knowledge upload endpoints the library page already uses.
 */
async function archiveReport(message: AgentMessage): Promise<void> {
  const result = message.reportResult
  if (!result?.attachment_id || reportBusy[message.id] || reportArchived[result.attachment_id]) return
  reportBusy[message.id] = 'archive'
  try {
    const blob = await fetchAgentReport(result.attachment_id)
    const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
    const hash = `sha256:${Array.from(new Uint8Array(digest)).map((item) => item.toString(16).padStart(2, '0')).join('')}`
    const requestID = `report-${result.attachment_id}`
    await createLocalUploadTask({
      requestID,
      traceID: `trace-${requestID}`,
      uploadDestination: 'private_local_library',
      fileName: result.file_name || '周报.docx',
      mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      sizeBytes: blob.size,
      contentHash: hash,
    })
    await uploadLocalContent(requestID, blob)
    reportArchived[result.attachment_id] = true
    MessagePlugin.success('已存入个人本地知识库')
  } catch (error) {
    MessagePlugin.error(error instanceof Error ? error.message : '存入知识库失败')
  } finally {
    delete reportBusy[message.id]
  }
}

async function chooseReportTemplate(
  result: ReportResult,
  candidate: ReportTemplateCandidate,
): Promise<void> {
  const chosen = candidate.origin === 'uploaded'
    ? candidate.attachment_id
    : candidate.preview_attachment_id || candidate.attachment_id
  if (!chosen) return
  const requestedName = String(result.file_name || '').trim()
  const nameHint = requestedName ? `，文件名叫${requestedName}` : ''
  question.value = `用这份模板给${result.subject_name || '我'}写周报${nameHint} [模板:${chosen}]`
  await sendMessage()
}

function applyRestoredApproval(message: AgentMessage, approval: AgentApproval): void {
  const args = approval.arguments || {}
  message.approval = approval
  approvalEditors[message.id] = {
    title: String(args.title || approval.capability || '待确认操作'),
    dueDate: typeof args.due_at === 'string'
      ? dayOfISO(args.due_at, String(args.timezone || 'Asia/Shanghai'))
      : '',
  }
  // The field list is seeded here too: an approval restored from conversation
  // history never passes through the live event path, and without this the
  // card rendered "0 字段" with no inputs at all.
  if (approval.capability === 'form.apply') {
    formEditors[message.id] = formFieldsFrom(args.draft)
  }
}

async function restoreAssistantMessage(
  historyMessage: AgentConversationMessage,
  approvals: AgentApproval[],
): Promise<AgentMessage> {
  const restoredStatus = statusFromHistoryMessage(historyMessage.status)
  const message: AgentMessage = {
    id: historyMessage.message_id,
    role: 'agent',
    taskId: historyMessage.task_id || undefined,
    status: restoredStatus,
    statusText: statusLabel(restoredStatus),
    steps: [],
    blocks: [],
    answer: historyMessage.content || '',
    citations: Array.isArray(historyMessage.citations)
      ? historyMessage.citations.map(mapCitation)
      : [],
    // Failed messages may carry the partial answer in content now; the error
    // text lives in task.last_error and is loaded below.
    error: undefined,
    lastEventId: 0,
  }
  if (!message.taskId) return message

  try {
    const [task, plan, observations] = await Promise.all([
      getAgentTask(message.taskId),
      getAgentPlan(message.taskId).catch(() => null),
      listAgentObservations(message.taskId).catch(() => [] as AgentObservation[]),
    ])
    message.taskId = task.task_id
    message.status = task.status || message.status
    message.statusText = statusLabel(message.status)
    message.steps = stepsFromPlan(plan)

    const taskAnswer = typeof task.result?.answer === 'string' ? task.result.answer : ''
    if (taskAnswer) message.answer = taskAnswer
    const resultCitations = Array.isArray(task.result?.citations) ? task.result.citations : null
    const citations = resultCitations || message.citations
    const evidence = evidenceForObservations(observations)
    message.citations = citations.map((value: any) => {
      const citation = mapCitation(value)
      return evidence.get(citation.evidence_id)
        ? { ...citation, ...evidence.get(citation.evidence_id) }
        : citation
    })
    const blocks = blocksFromObservations(observations, message.answer, message.citations)
    if (blocks.length) message.blocks = blocks
    message.todo = todoFromObservations(observations)
    // A generated weekly report has no answer text: its result is the card.
    // Rebuild it (and its sources) from the stored step output, otherwise the
    // document disappears as soon as the user leaves and re-opens the chat.
    const reportStep = [...observations]
      .reverse()
      .find(
        (item) => item.capability === 'report.weekly'
          && item.status === 'succeeded'
          && item.output,
      )
    if (reportStep?.output) {
      message.reportResult = reportResultFromPayload(reportStep.output as Record<string, any>)
      const reportCitations = citationsFromPayload(reportStep.output as Record<string, any>)
      if (reportCitations.length) message.citations = reportCitations
    }

    if (message.status === 'waiting_approval') {
      const approval = approvals.find(
        (item) => item.task_id === message.taskId
          && (item.status === 'waiting_approval' || item.status === 'expired'),
      )
      if (approval) applyRestoredApproval(message, approval)
    } else if (message.status === 'waiting_input') {
      const waiting = observations.find(
        (item) => item.output?.requires_user_input === true,
      )
      message.inputRequest = {
        missing: Array.isArray(waiting?.output?.missing_information)
          ? waiting.output.missing_information.map(String)
          : [],
      }
    }
    if (task.last_error?.message) message.error = String(task.last_error.message)
  } catch {
    // The persisted message remains usable even if its execution detail is gone.
  }
  if (!message.error && historyMessage.status === 'failed') {
    message.error = '任务执行失败'
  }
  return message
}

async function restoreConversationMessages(
  detail: AgentConversationDetail,
  approvals: AgentApproval[],
): Promise<AgentMessage[]> {
  const restored: AgentMessage[] = []
  for (const item of detail.messages || []) {
    if (item.role === 'user') {
      restored.push({
        id: item.message_id,
        role: 'user',
        text: item.content,
        steps: [],
        blocks: [],
        citations: [],
        lastEventId: 0,
      })
      continue
    }
    if (item.role !== 'assistant') continue
    restored.push(await restoreAssistantMessage(item, approvals))
  }
  return restored
}

function resetConversationState(): void {
  activeController?.abort()
  activeController = null
  activeTaskID.value = ''
  question.value = ''
  selectedFile.value = null
  if (fileInputRef.value) fileInputRef.value.value = ''
  sourcePreviewVisible.value = false
  activeSourceFile.value = null
  activeSourceTarget.value = null
  messages.value = []
  for (const key of Object.keys(approvalEditors)) delete approvalEditors[key]
  for (const key of Object.keys(inputValues)) delete inputValues[key]
  for (const key of Object.keys(expandedTraces)) delete expandedTraces[key]
  for (const key of Object.keys(expandedSources)) delete expandedSources[key]
}

async function loadConversation(id: string): Promise<void> {
  const sequence = ++conversationLoadSequence
  resetChatScroll()
  resetConversationState()
  if (!id) {
    conversationId.value = ''
    restoringConversation.value = false
    await nextTick()
    await jumpToLatest()
    return
  }

  restoringConversation.value = true
  try {
    const detail = await getAgentConversation(id)
    const approvals = await listAgentApprovals().catch(() => [] as AgentApproval[])
    const restored = await restoreConversationMessages(detail, approvals)
    if (sequence !== conversationLoadSequence) return
    conversationId.value = id
    messages.value = restored
    const resumable = [...restored].reverse().find(
      (item) => item.role === 'agent'
        && item.taskId
        && ['received', 'planning', 'ready', 'executing'].includes(item.status || ''),
    )
    if (resumable) void watchTask(resumable, 0)
  } catch (error) {
    if (sequence !== conversationLoadSequence) return
    conversationId.value = id
    messages.value = []
    MessagePlugin.error(error instanceof Error ? error.message : '会话历史加载失败')
  } finally {
    if (sequence === conversationLoadSequence) restoringConversation.value = false
  }
  await jumpToLatest()
}

async function syncConversationFromRoute(): Promise<void> {
  const target = route.name === 'chat' ? String(route.query.conversation || '') : ''
  if (target === conversationId.value) return
  await loadConversation(target)
}

async function hydrateTerminal(message: AgentMessage): Promise<void> {
  if (!message.taskId) return
  try {
    const [task, observations] = await Promise.all([
      getAgentTask(message.taskId),
      listAgentObservations(message.taskId),
    ])
    if (!message.answer && typeof task.result?.answer === 'string') message.answer = task.result.answer
    const evidence = evidenceForObservations(observations)
    const citations = Array.isArray(task.result?.citations) ? task.result.citations : message.citations
    message.citations = citations.map((value: any) => {
      const citation = mapCitation(value)
      return evidence.get(citation.evidence_id) ? { ...citation, ...evidence.get(citation.evidence_id) } : citation
    })
    const blocks = blocksFromObservations(observations, message.answer, message.citations)
    if (blocks.length) message.blocks = blocks
    if (!message.todo) message.todo = todoFromObservations(observations)
    if (task.last_error?.message) message.error = String(task.last_error.message)
  } catch {
    // The terminal event already carries the user-visible result when available.
  }
  scheduleChatFollow()
}

async function watchTask(message: AgentMessage, after = message.lastEventId): Promise<void> {
  if (!message.taskId) return
  activeController?.abort()
  activeController = new AbortController()
  activeTaskID.value = message.taskId
  try {
    await streamAgentTaskEvents(
      message.taskId,
      {
        onEvent: (event) => handleTaskEvent(message, event),
        onWaiting: () => {
          message.statusText = '继续等待'
        },
        onReconnect: (attempt) => {
          message.statusText = `正在恢复连接（${attempt}）`
        },
        onAnswerCursor: ({ answerId, answerAfter }) => {
          if (answerId) message.answerId = answerId
          message.answerNextSeq = Math.max(1, Number(answerAfter || 0) + 1)
        },
      },
      {
        after,
        answerId: message.answerId,
        answerAfter: Math.max(0, Number(message.answerNextSeq || 1) - 1),
        signal: activeController.signal,
      },
    )
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError')) {
      message.status = 'failed'
      message.statusText = '连接失败'
      message.error = error instanceof Error ? error.message : 'Agent 流式连接失败'
      MessagePlugin.error('Agent 流式连接失败')
    }
  } finally {
    if (activeTaskID.value === message.taskId) activeTaskID.value = ''
    notifyConversationChanged()
  }
  scheduleChatFollow()
}

async function sendMessage(): Promise<void> {
  const text = question.value.trim()
  if (!text || activeTaskID.value || uploading.value) return
  const file = selectedFile.value
  let attachmentIds: string[] = []
  let attachments: { id: string; name: string }[] = []
  if (file) {
    uploading.value = true
    try {
      const uploaded = await uploadAgentAttachment(file)
      attachmentIds = [uploaded.attachment_id]
      attachments = [{ id: String(uploaded.attachment_id), name: file.name }]
    } catch (error) {
      uploading.value = false
      MessagePlugin.error(error instanceof Error ? error.message : '附件上传失败')
      return
    }
    uploading.value = false
    selectedFile.value = null
    if (fileInputRef.value) fileInputRef.value.value = ''
  }
  const userMessage: AgentMessage = { id: newMessageID(), role: 'user', text, attachments, steps: [], blocks: [], citations: [], lastEventId: 0 }
  const agentMessage = reactive<AgentMessage>({
    id: newMessageID(),
    role: 'agent',
    status: 'received',
    statusText: '正在创建任务',
    steps: [],
    blocks: [],
    citations: [],
    lastEventId: 0,
  })
  messages.value.push(userMessage, agentMessage)
  question.value = ''
  await jumpToLatest()
  try {
    const created = await createAgentTask({
      text,
      attachmentIds,
      conversationId: conversationId.value || undefined,
    })
    if (created.conversation_id && created.conversation_id !== conversationId.value) {
      conversationId.value = created.conversation_id
      if (route.name === 'chat') {
        await router.replace({
          path: '/chat',
          query: { ...route.query, conversation: created.conversation_id },
        })
      }
    }
    agentMessage.taskId = created.task_id
    agentMessage.status = created.status
    agentMessage.statusText = statusLabel(created.status)
    notifyConversationChanged()
    await watchTask(agentMessage, 0)
  } catch (error) {
    agentMessage.status = 'failed'
    agentMessage.statusText = '任务创建失败'
    agentMessage.error = error instanceof Error ? error.message : '无法创建 Agent Task'
    MessagePlugin.error(agentMessage.error)
  } finally {
    activeTaskID.value = ''
    notifyConversationChanged()
    scheduleChatFollow()
  }
}

async function copyText(text: string, successMessage: string): Promise<void> {
  const value = String(text || '').trim()
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    MessagePlugin.success(successMessage)
  } catch {
    MessagePlugin.info('复制失败，请检查浏览器剪贴板权限')
  }
}

function editQuestion(text: string): void {
  if (!String(text || '').trim()) return
  question.value = text
  void nextTick(() => textareaRef.value?.focus?.())
}

async function stopActiveTask(): Promise<void> {
  const message = messages.value.find((item) => item.taskId === activeTaskID.value)
  if (!message) return
  const cancelled = await cancelTask(message)
  if (!cancelled) return
  activeController?.abort()
  activeController = null
  activeTaskID.value = ''
}

function handleFileChange(value: Event): void {
  const input = value.target as HTMLInputElement
  selectedFile.value = input.files?.[0] || null
}

function openFilePicker(): void {
  fileInputRef.value?.click()
}

function clearFile(): void {
  selectedFile.value = null
  if (fileInputRef.value) fileInputRef.value.value = ''
}

function approvalEditor(message: AgentMessage) {
  return approvalEditors[message.id] || { title: '', dueDate: '' }
}

function formFieldsFrom(draft: unknown): FormEditorField[] {
  const raw =
    draft && typeof draft === 'object' && Array.isArray((draft as Record<string, unknown>).fields)
      ? ((draft as Record<string, unknown>).fields as unknown[])
      : []
  return raw.map((item) => {
    const field = item && typeof item === 'object' ? (item as Record<string, unknown>) : {}
    return {
      name: String(field.name ?? ''),
      value: String(field.value ?? ''),
      source: String(field.source ?? 'empty'),
      // Which subject the value was scoped to, how strong that attribution is
      // ("b" = shared source, needs a human look) and where it came from.
      subject: String(field.subject ?? ''),
      tier: String(field.tier ?? 'none'),
      evidence: String(field.evidence ?? ''),
    }
  })
}

/** Where a draft value came from, in the owner's words. */
function formSourceLabel(field: { source: string; subject?: string; tier?: string }): string {
  const base =
    (
      {
        instruction: '来自指令',
        knowledge: '来自知识库',
        user: '你填写的',
        page: '页面已有',
        empty: '待补充',
      } as Record<string, string>
    )[field.source] || '待补充'
  if (field.source !== 'knowledge') return base
  const subject = String(field.subject || '').trim()
  const label = subject ? `来自「${subject}」` : base
  return field.tier === 'b' ? `${label} · 请核对` : label
}

/** Chip tint: "请核对" outranks the plain retrieval tint. */
function formSourceKind(field: { source: string; tier?: string }): string {
  if (field.tier === 'b') return 'review'
  if (field.source === 'knowledge') return 'knowledge'
  if (field.source === 'user') return 'user'
  if (field.source === 'empty') return 'empty'
  return 'instruction'
}

function formEditor(message: AgentMessage) {
  return formEditors[message.id] || []
}

function formDraft(message: AgentMessage): Record<string, any> {
  const draft = message.approval?.arguments?.draft
  return draft && typeof draft === 'object' ? (draft as Record<string, any>) : {}
}

function formSummary(message: AgentMessage) {
  const fields = formEditor(message)
  const filled = fields.filter((field) => field.value.trim()).length
  return { total: fields.length, filled, empty: fields.length - filled }
}

/** Share of fields that already hold a value, for the meter and its label. */
function formCompletion(message: AgentMessage): number {
  const summary = formSummary(message)
  if (!summary.total) return 0
  return Math.round((summary.filled / summary.total) * 100)
}

/**
 * Whether any value came from retrieval.
 *
 * Semantic search has no notion of context or ordering, so a retrieved value
 * can be the wrong one for the field. The card is the safety net, and it only
 * works if it says so.
 */
function formHasRetrieved(message: AgentMessage): boolean {
  return formEditor(message).some((field) => field.source === 'knowledge')
}

/** Values taken from a shared carrier, which the owner has to check by eye. */
function formHasReview(message: AgentMessage): boolean {
  return formEditor(message).some((field) => field.tier === 'b')
}

/** The sidecar captures at this viewport, so clicks map back through it. */
const TAKEOVER_VIEWPORT = { width: 1440, height: 900 }
const takeoverFrames = reactive<Record<string, string>>({})
const takeoverText = reactive<Record<string, string>>({})
const takeoverBusy = reactive<Record<string, boolean>>({})
const takeoverTimers = new Map<string, number>()

/** Undo availability for the write receipt, and whether it was taken. */
const formUndo = reactive<Record<string, boolean>>({})
const formUndone = reactive<Record<string, boolean>>({})
const undoBusy = reactive<Record<string, boolean>>({})

async function refreshFormUndo(message: AgentMessage): Promise<void> {
  if (!message.taskId) return
  try {
    const state = await getFormUndo(message.taskId)
    formUndo[message.id] = Boolean(state.available)
  } catch {
    formUndo[message.id] = false
  }
}

async function undoForm(message: AgentMessage): Promise<void> {
  if (!message.taskId || undoBusy[message.id]) return
  undoBusy[message.id] = true
  try {
    await undoFormWrite(message.taskId)
    formUndo[message.id] = false
    formUndone[message.id] = true
  } catch (error) {
    message.error = error instanceof Error ? error.message : '撤销失败'
    MessagePlugin.error(message.error)
  } finally {
    undoBusy[message.id] = false
  }
}

function isTakeover(message: AgentMessage): boolean {
  return Boolean(message.taskId) && Boolean(message.inputRequest?.missing?.includes('form_login'))
}

async function refreshTakeoverFrame(message: AgentMessage): Promise<void> {
  if (!message.taskId) return
  try {
    const url = await fetchTakeoverFrame(message.taskId)
    const previous = takeoverFrames[message.id]
    takeoverFrames[message.id] = url
    if (previous) URL.revokeObjectURL(previous)
  } catch {
    // A single missed frame is not fatal; the next tick retries.
  }
}

function startTakeover(message: AgentMessage): void {
  if (!isTakeover(message) || takeoverTimers.has(message.id)) return
  void refreshTakeoverFrame(message)
  takeoverTimers.set(
    message.id,
    window.setInterval(() => void refreshTakeoverFrame(message), 1500),
  )
}

function stopTakeover(message: AgentMessage): void {
  const timer = takeoverTimers.get(message.id)
  if (timer !== undefined) {
    window.clearInterval(timer)
    takeoverTimers.delete(message.id)
  }
  const frame = takeoverFrames[message.id]
  if (frame) {
    URL.revokeObjectURL(frame)
    takeoverFrames[message.id] = ''
  }
}

async function sendTakeover(
  message: AgentMessage,
  input: Parameters<typeof sendAgentTakeoverInput>[1],
): Promise<void> {
  if (!message.taskId || takeoverBusy[message.id]) return
  takeoverBusy[message.id] = true
  try {
    await sendAgentTakeoverInput(message.taskId, input)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '接管操作失败'
  } finally {
    takeoverBusy[message.id] = false
    await refreshTakeoverFrame(message)
  }
}

async function takeoverClick(message: AgentMessage, event: MouseEvent): Promise<void> {
  const image = event.currentTarget as HTMLImageElement
  const rect = image.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  await sendTakeover(message, {
    kind: 'click',
    x: ((event.clientX - rect.left) / rect.width) * TAKEOVER_VIEWPORT.width,
    y: ((event.clientY - rect.top) / rect.height) * TAKEOVER_VIEWPORT.height,
  })
}

async function takeoverSendText(message: AgentMessage): Promise<void> {
  const text = (takeoverText[message.id] || '').trim()
  if (!text) return
  await sendTakeover(message, { kind: 'type', text })
  takeoverText[message.id] = ''
}

async function takeoverPressEnter(message: AgentMessage): Promise<void> {
  await sendTakeover(message, { kind: 'key', text: 'Enter' })
}

async function continueAfterTakeover(message: AgentMessage): Promise<void> {
  stopTakeover(message)
  if (!message.taskId || message.submitting) return
  message.submitting = true
  try {
    await submitAgentTaskInput(message.taskId, {
      text: '已完成登录，请继续',
      resume: true,
    })
    message.inputRequest = undefined
    message.status = 'executing'
    message.statusText = '登录已完成，正在继续原任务'
    await watchTask(message, message.lastEventId)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '继续任务失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function confirmApproval(message: AgentMessage): Promise<void> {
  if (!message.approval || message.submitting) return
  message.submitting = true
  try {
    const approval = message.approval
    const editor = approvalEditor(message)
    let arguments_: Record<string, unknown> = approval.arguments || {}
    if (approval.capability === 'todo.create') {
      const draft = buildScheduleDraft({ task: { task_id: message.taskId || '', source_type: 'chat', owner_user_id: '', status: 'waiting_approval' }, approval })
      const edited = composeEditedArguments(draft, { title: editor.title, dueDate: editor.dueDate })
      arguments_ = { ...approval.arguments, ...edited.arguments, title: editor.title }
    }
    if (approval.capability === 'form.apply') {
      // Send back exactly what is on the card: the backend overwrites the Step
      // arguments with this and re-fingerprints before writing.
      const original = formDraft(message)
      const originalFields = Array.isArray(original.fields) ? (original.fields as Array<Record<string, any>>) : []
      const fields = formEditor(message).map((field, index) => {
        const before: Record<string, any> = originalFields[index] || {}
        const value = field.value.trim()
        const untouched = field.value === String(before.value ?? '')
        return {
          ...before,
          name: field.name || String(before.name ?? ''),
          value: field.value,
          // Keep the Agent's provenance while the owner has not touched it;
          // an edited value is theirs, and a cleared one is missing again.
          source: value ? (untouched ? field.source || 'instruction' : 'user') : 'empty',
          confidence: value ? 1 : 0,
        }
      })
      const draft: Record<string, any> = { ...original, fields }
      draft.missing = fields.filter((field) => !String(field.value || '').trim()).map((field) => String(field.name || ''))
      arguments_ = {
        ...approval.arguments,
        draft,
        values: [fields.map((field) => String(field.value || ''))],
        action: draft.write_model === 'live_document' ? 'write_cells' : approval.arguments?.action || 'write_cells',
      }
    }
    await approveAgentApproval(approval.approval_id, approval.version, arguments_)
    message.approval = undefined
    message.status = 'executing'
    message.statusText = '已确认，正在执行'
    message.lastEventId = Math.max(message.lastEventId, 0)
    await watchTask(message, message.lastEventId)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '确认失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function rejectApproval(message: AgentMessage): Promise<void> {
  if (!message.approval || message.submitting) return
  message.submitting = true
  try {
    await rejectAgentApproval(message.approval.approval_id, message.approval.version)
    message.approval = undefined
    message.status = 'failed'
    message.statusText = '审批已拒绝'
    message.error = '审批已拒绝，任务不会执行'
  } catch (error) {
    message.error = error instanceof Error ? error.message : '拒绝失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function submitInput(message: AgentMessage): Promise<void> {
  const value = (inputValues[message.id] || '').trim()
  if (!message.taskId || !value || message.submitting) return
  message.submitting = true
  try {
    await submitAgentTaskInput(message.taskId, { text: value })
    inputValues[message.id] = ''
    message.inputRequest = undefined
    message.status = 'planning'
    message.statusText = '已提交补充信息'
    await watchTask(message, message.lastEventId)
  } catch (error) {
    message.error = error instanceof Error ? error.message : '补充信息提交失败'
    MessagePlugin.error(message.error)
  } finally {
    message.submitting = false
  }
}

async function cancelTask(message: AgentMessage): Promise<boolean> {
  if (!message.taskId) return false
  try {
    const task = await cancelAgentTask(message.taskId)
    message.status = task.status
    message.statusText = statusLabel(task.status)
    return true
  } catch (error) {
    message.error = error instanceof Error ? error.message : '取消失败'
    return false
  }
}

function handleTextareaKeydown(value: unknown, context?: { e?: KeyboardEvent }): void {
  const event = context?.e || (value instanceof KeyboardEvent ? value : undefined)
  if (!event || event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  void sendMessage()
}

onMounted(() => {
  void syncConversationFromRoute()
})

watch(
  () => route.query.conversation,
  () => {
    void syncConversationFromRoute()
  },
)

onBeforeUnmount(() => {
  messages.value.forEach((message) => stopTakeover(message))
  conversationLoadSequence += 1
  activeController?.abort()
  disposeChatScroll()
})
</script>

<style lang="less" scoped>
.agent-chat-page { position: relative; display: flex; flex-direction: column; width: 100%; height: 100%; min-height: 620px; background: var(--td-bg-color-container); }
.agent-chat-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 32px 34px 200px; }
.agent-welcome { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100%; padding-bottom: 120px; text-align: center; }
.agent-welcome h1 { margin: 0 0 12px; color: var(--td-text-color-primary); font-size: 36px; font-weight: 600; }
.agent-welcome p { margin: 0; color: var(--td-text-color-secondary); font-size: 16px; }
.agent-transcript { width: min(920px, 100%); margin: 0 auto; padding-bottom: 24px; }
.agent-message { display: flex; width: 100%; margin-bottom: 38px; }
.agent-message--user { justify-content: flex-end; }
.agent-response { position: relative; display: grid; width: 100%; gap: 12px; }
.agent-response__header { display: flex; align-items: center; gap: 10px; }
.agent-response__mark { display: grid; width: 28px; height: 28px; flex: 0 0 28px; place-items: center; color: var(--td-brand-color); background: transparent; }
.agent-response__mark svg { width: 15px; height: 15px; }
.agent-response__identity { display: flex; align-items: baseline; gap: 9px; min-width: 0; }
.agent-response__identity strong { color: var(--td-text-color-primary); font-size: 13px; font-weight: 600; }
.agent-answer { display: grid; width: min(820px, calc(100% - 38px)); gap: 14px; margin-left: 38px; }
.agent-answer__interrupted { display: grid; gap: 8px; padding: 10px 12px; border: 1px dashed var(--td-component-stroke); border-radius: 10px; opacity: .72; }
.agent-answer__interrupted-label { justify-self: start; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-answer__interrupted :deep(.agent-answer-content) { font-size: 14px; line-height: 1.7; }
.agent-user-actions, .agent-answer__actions { display: flex; align-items: center; gap: 7px; margin-top: 12px; opacity: 0; pointer-events: none; transition: opacity .15s ease; }
.agent-answer__actions { justify-content: flex-end; margin-top: 4px; }
.agent-user-actions { justify-content: flex-end; margin-top: 0; gap: 4px; }
.agent-user-message:hover .agent-user-actions, .agent-user-message:focus-within .agent-user-actions,
.agent-response:hover .agent-answer__actions, .agent-response:focus-within .agent-answer__actions { opacity: 1; pointer-events: auto; }
.agent-user-actions button, .agent-answer__actions button { display: inline-grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 6px; color: var(--td-text-color-placeholder); background: transparent; cursor: pointer; }
.agent-user-actions button:hover, .agent-answer__actions button:hover { color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); }
.agent-user-actions svg, .agent-answer__actions svg { width: 16px; height: 16px; }
.agent-status { display: inline-flex; align-items: center; gap: 7px; margin-bottom: 10px; color: var(--td-text-color-secondary); font-size: 13px; }
.agent-status__dot { width: 7px; height: 7px; border-radius: 50%; background: var(--td-brand-color); }
.agent-status--succeeded .agent-status__dot { background: var(--td-success-color); }
.agent-status--failed .agent-status__dot, .agent-status--unknown .agent-status__dot { background: var(--td-error-color); }
.agent-status--waiting_approval .agent-status__dot, .agent-status--waiting_input .agent-status__dot { background: var(--td-warning-color); }
.agent-steps { display: grid; gap: 5px; margin: 0 0 12px; padding: 0; list-style: none; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-steps li { display: flex; align-items: center; gap: 6px; }
.agent-steps li::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--td-brand-color-disabled); }
.agent-step--succeeded::before { background: var(--td-success-color) !important; }
.agent-step--failed::before { background: var(--td-error-color) !important; }
.agent-empty-result { display: grid; gap: 5px; margin-top: 0; padding: 14px 15px; border: 1px dashed var(--td-component-stroke); border-radius: 12px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 13px; line-height: 1.6; }
.agent-empty-result strong { color: var(--td-text-color-primary); font-size: 13px; font-weight: 500; }
.agent-empty-result span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-sources { display: grid; gap: 8px; margin-top: 16px; }
.agent-sources__toggle { display: inline-flex; align-items: center; justify-content: flex-start; width: fit-content; min-height: 28px; gap: 10px; padding: 4px 8px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-brand-color); background: var(--td-brand-color-1); font-size: 12px; cursor: pointer; }
.agent-sources__toggle > span { display: inline-flex; align-items: center; gap: 6px; }
.agent-sources__toggle > svg { width: 14px; height: 14px; }
.agent-sources__list { display: grid; gap: 8px; }
.agent-sources__header { display: flex; align-items: center; gap: 8px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-trace { margin-top: 14px; }
.agent-trace__toggle { display: inline-flex; align-items: center; gap: 6px; padding: 0; border: 0; color: var(--td-text-color-secondary); background: transparent; font-size: 12px; cursor: pointer; }
.agent-trace__toggle svg { transition: transform .15s ease; }
.agent-trace__chevron--open { transform: rotate(90deg); }
.agent-trace__steps { display: grid; gap: 6px; margin: 9px 0 0; padding: 0; list-style: none; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-trace__steps li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 9px; border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }
.agent-step__body { display: grid; gap: 3px; }
.agent-step__stage { color: var(--td-text-color-placeholder); font-style: normal; font-size: 12px; }
.agent-trace__steps small { color: var(--td-text-color-placeholder); }
.agent-result-blocks { display: grid; gap: 14px; }
.agent-result-heading { display: flex; align-items: center; gap: 7px; margin-bottom: 9px; color: var(--td-text-color-primary); font-size: 13px; font-weight: 600; }
.agent-result-heading svg { color: var(--td-brand-color); }
.agent-empty-result { margin: 0; padding: 12px; border: 1px dashed var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-secondary); font-size: 13px; }
.agent-source-list, .agent-content-results { display: grid; gap: 8px; }
.agent-source-card, .agent-content-card { display: flex; justify-content: space-between; gap: 12px; padding: 12px; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.agent-source-card { cursor: pointer; transition: border-color .15s, background .15s; }
.agent-source-card:hover { border-color: var(--td-brand-color-focus); background: var(--td-bg-color-container-hover); }
.agent-source-card__body, .agent-content-card__title { min-width: 0; }
.agent-source-card strong, .agent-content-card strong { display: block; overflow: hidden; color: var(--td-text-color-primary); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.agent-source-card span, .agent-content-card span { display: block; margin-top: 4px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-source-card p { display: -webkit-box; overflow: hidden; margin: 7px 0 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.agent-source-card__actions, .agent-citation__actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; align-items: flex-start; gap: 6px; }
.agent-source-card__actions span { display: inline-block; min-height: 24px; margin: 0; padding: 0 8px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-brand-color); background: var(--td-bg-color-container); font-size: 12px; line-height: 22px; }
.agent-source-card__actions button, .agent-citation__actions button, .agent-citation__actions a { min-height: 28px; padding: 0 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-brand-color); background: var(--td-bg-color-container); font-size: 12px; line-height: 26px; text-decoration: none; cursor: pointer; }
.agent-content-card { display: block; }
.agent-content-card blockquote { margin: 9px 0 0; padding: 9px 10px; border-left: 1px solid var(--td-brand-color-focus); border-radius: 0 6px 6px 0; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 12px; line-height: 1.6; }
.agent-content-card .agent-source-card__actions { margin-top: 9px; }
.agent-coverage-note { margin: 2px 0 0; color: var(--td-warning-color); font-size: 12px; line-height: 1.5; }
.agent-answer-block { display: grid; gap: 14px; }
.agent-answer__content { color: var(--td-text-color-primary); font-size: 15px; line-height: 1.8; }
.agent-answer__content :deep(p) { margin: 0 0 10px; }
.agent-answer__content :deep(pre) { overflow-x: auto; padding: 10px 12px; border-radius: 6px; background: var(--td-bg-color-secondarycontainer); }
.agent-citations { display: grid; gap: 8px; margin-top: 14px; }
.agent-citation { display: flex; gap: 9px; padding: 10px 12px; border: 1px solid var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-primary); }
.agent-citation > svg { flex: 0 0 auto; color: var(--td-brand-color); }
.agent-citation strong { display: block; font-size: 13px; }
.agent-citation p { margin: 4px 0 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.5; }
.agent-citation small { display: block; margin-top: 4px; color: var(--td-text-color-placeholder); font-size: 11px; }
.agent-citation a { display: block; overflow: hidden; margin-top: 4px; color: var(--td-brand-color); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.agent-citation__actions { margin-top: 7px; }
.agent-todo { display: grid; gap: 5px; margin-top: 2px; padding: 14px 16px; border: 1px solid var(--td-brand-color-focus); border-radius: 12px; background: var(--td-brand-color-1); }
.agent-todo__title { display: flex; align-items: center; gap: 6px; color: var(--td-brand-color); font-size: 12px; }
.agent-todo strong { font-size: 15px; }
.agent-todo span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form-result { display: grid; gap: 4px; margin-top: 2px; padding: 14px 16px; border: 1px solid var(--td-success-color-3); border-radius: 12px; background: var(--td-success-color-1); }
.agent-form-result__title { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.agent-form-result span { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form-result__undo { justify-self: start; min-height: 30px; padding: 0 12px; }
.agent-report { display: grid; gap: 10px; margin-top: 2px; padding: 14px 16px; border: 1px solid var(--td-brand-color-3); border-radius: 12px; background: var(--td-bg-color-container); }
.agent-report--no_template, .agent-report--no_person, .agent-report--needs_person_choice { border-color: var(--td-warning-color-3); background: var(--td-warning-color-1); }
.agent-report__head { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; }
.agent-report__meta { display: flex; flex-wrap: wrap; gap: 4px 14px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-report__actions { display: flex; flex-wrap: wrap; gap: 8px; }
.agent-report__sources { display: grid; gap: 6px; margin: 0; padding: 10px 0 0; border-top: 1px solid var(--td-component-stroke); list-style: none; }
.agent-report__sources li { display: grid; gap: 2px; min-width: 0; }
.agent-report__source-title { color: var(--td-text-color-primary); font-size: 12px; font-weight: 600; }
.agent-report__source-quote { overflow: hidden; color: var(--td-text-color-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.agent-report__candidates { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.agent-report__candidates li { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.agent-report__candidate-actions { display: inline-flex; gap: 4px; }
.agent-approval, .agent-input-request { display: grid; gap: 11px; margin-top: 2px; padding: 16px; border: 1px solid var(--td-warning-color-3); border-radius: 12px; background: var(--td-warning-color-1); }
.agent-approval__header, .agent-approval__actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.agent-approval__header span { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.agent-approval__header small { color: var(--td-text-color-secondary); }
.agent-approval label, .agent-input-request label { display: grid; gap: 5px; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-approval input, .agent-input-request input { width: 100%; min-height: 34px; padding: 6px 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); }
.agent-approval__arguments { max-height: 220px; overflow: auto; margin: 0; padding: 10px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); font-size: 12px; }
/* Form prefill is a light glass panel instead of a warning-coloured slab: the
   "needs confirmation" signal survives as a hairline rail on the left, and the
   panel stays translucent so it reads as part of the page. */
.agent-approval.agent-approval--form { border-color: var(--td-component-border); background: color-mix(in srgb, var(--td-bg-color-container) 88%, transparent); backdrop-filter: blur(14px) saturate(140%); box-shadow: 0 1px 2px rgba(0, 0, 0, .04), 0 10px 24px -16px rgba(0, 0, 0, .18); }
.agent-form { position: relative; display: grid; gap: 12px; padding-left: 14px; }
.agent-form::before { content: ''; position: absolute; top: 3px; bottom: 3px; left: 0; width: 3px; border-radius: 2px; background: color-mix(in srgb, var(--td-warning-color) 45%, transparent); }
.agent-form__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.agent-form__heading { display: grid; gap: 2px; min-width: 0; }
.agent-form__title { margin: 0; color: var(--td-text-color-primary); font-size: 15px; font-weight: 600; }
.agent-form__summary { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form__percent { color: var(--td-text-color-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.agent-form__progress { height: 2px; overflow: hidden; border-radius: 999px; background: color-mix(in srgb, var(--td-component-stroke) 60%, transparent); }
.agent-form__progress > span { display: block; height: 100%; border-radius: inherit; background: var(--td-brand-color); transition: width 200ms ease; }
.agent-form__warning { margin: 0; color: var(--td-warning-color-6, #e37318); font-size: 12px; }
.agent-form__hint { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; }
.agent-form__fields { display: grid; gap: 10px; }
.agent-form__fields label { display: grid; grid-template-columns: minmax(88px, 120px) minmax(0, 1fr); align-items: start; gap: 6px 12px; color: var(--td-text-color-secondary); font-size: 13px; }
.agent-form__label { display: flex; align-items: center; min-height: 36px; color: var(--td-text-color-primary); }
.agent-form__meta { grid-column: 2; display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; min-width: 0; }
.agent-form__source { padding: 1px 7px; border-radius: 999px; color: var(--td-text-color-placeholder); background: var(--td-bg-color-component); font-size: 11px; white-space: nowrap; }
.agent-form__source--knowledge { color: var(--td-brand-color-8); background: var(--td-brand-color-1); }
.agent-form__source--review { color: var(--td-warning-color-8); background: var(--td-warning-color-1); }
.agent-form__evidence { color: var(--td-text-color-placeholder); font-size: 11px; font-style: normal; overflow-wrap: anywhere; }
.agent-form__fields input { width: 100%; min-height: 36px; padding: 7px 10px; border: 1px solid color-mix(in srgb, var(--td-component-stroke) 75%, transparent); border-radius: var(--td-radius-large); color: var(--td-text-color-primary); background: color-mix(in srgb, var(--td-bg-color-container) 55%, transparent); transition: border-color 150ms ease, box-shadow 150ms ease; }
.agent-form__fields input:focus { outline: none; border-color: var(--td-brand-color); box-shadow: 0 0 0 3px var(--td-brand-color-focus); }
@media (max-width: 760px) { .agent-form__fields label { grid-template-columns: 1fr; } .agent-form__label { min-height: 0; } .agent-form__meta { grid-column: 1; } }
.agent-takeover { display: grid; gap: 10px; margin-top: 2px; padding: 16px; border: 1px solid var(--td-warning-color-3); border-radius: 12px; background: var(--td-warning-color-1); }
.agent-takeover__hint { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.6; }
.agent-takeover__screen { width: 100%; max-height: 420px; object-fit: contain; border: 1px solid var(--td-component-stroke); border-radius: 6px; background: #fff; cursor: crosshair; }
.agent-takeover__controls { display: grid; grid-template-columns: 1fr auto auto; gap: 8px; }
.agent-takeover__controls input { width: 100%; min-height: 34px; padding: 6px 9px; border: 1px solid var(--td-component-stroke); border-radius: 6px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); }
.agent-button { min-height: 34px; padding: 0 14px; border: 1px solid var(--td-component-stroke); border-radius: 8px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); cursor: pointer; transition: border-color 150ms ease, background 150ms ease, color 150ms ease; }
.agent-button:hover:not(:disabled) { border-color: var(--td-brand-color-focus); background: var(--td-bg-color-container-hover); }
.agent-button--primary { border-color: var(--td-brand-color); color: #fff; background: var(--td-brand-color); }
.agent-button:disabled { cursor: not-allowed; opacity: .55; }
.agent-input-request { grid-template-columns: 1fr auto; align-items: end; }
.agent-input-request label { grid-column: 1; }
.agent-input-request button { grid-column: 2; align-self: end; }
.agent-error { margin: 2px 0 0; padding: 11px 13px; border: 1px solid var(--td-error-color-3); border-radius: 10px; color: var(--td-error-color); background: var(--td-error-color-1); font-size: 13px; line-height: 1.6; }
.agent-composer-area { position: absolute; z-index: 5; right: 0; bottom: 0; left: 0; display: flex; flex-direction: column; align-items: center; padding: 0 24px 22px; background: linear-gradient(to top, var(--td-bg-color-container) 62%, transparent); }
.agent-scroll-latest { position: absolute; z-index: 6; bottom: 176px; left: 50%; display: inline-flex; align-items: center; min-height: 34px; gap: 6px; padding: 0 12px; border: 1px solid var(--td-component-stroke); border-radius: 17px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); box-shadow: 0 4px 14px rgba(0, 0, 0, .1); font-size: 12px; cursor: pointer; transform: translateX(-50%); }
.agent-scroll-latest:hover { border-color: var(--td-brand-color); color: var(--td-brand-color); }
.agent-scroll-latest:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
.agent-scroll-latest svg { width: 15px; height: 15px; }
.agent-composer { position: relative; width: min(960px, 100%); border: 1px solid var(--td-component-stroke); border-radius: 14px; background: var(--td-bg-color-container); box-shadow: 0 2px 8px rgba(0, 0, 0, .04), 0 8px 16px -4px rgba(0, 0, 0, .06); transition: border-color .15s, box-shadow .15s; }
.agent-composer--focused { border-color: var(--td-brand-color); box-shadow: 0 0 0 3px var(--td-brand-color-focus), 0 8px 18px -8px rgba(0, 0, 0, .18); }
.agent-textarea :deep(.t-textarea__inner) { min-height: 112px; padding: 16px 18px 56px; border: 0; border-radius: 14px; resize: none; color: var(--td-text-color-primary); font-size: 16px; line-height: 1.5; box-shadow: none; }
.agent-textarea :deep(.t-textarea__inner:focus) { box-shadow: none; }
.agent-composer__controls { position: absolute; right: 14px; bottom: 12px; left: 14px; display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.agent-composer__left { display: flex; align-items: center; min-width: 0; gap: 8px; }
.agent-attach { display: grid; place-items: center; width: 30px; height: 30px; padding: 0; border: 0; border-radius: 8px; color: var(--td-text-color-secondary); background: transparent; cursor: pointer; }
.agent-attach:hover { color: var(--td-brand-color); background: var(--td-bg-color-secondarycontainer); }
.agent-attach:disabled { cursor: not-allowed; opacity: .5; }
.agent-file-input { position: absolute; width: 1px; height: 1px; overflow: hidden; opacity: 0; pointer-events: none; }
.agent-file-chip { display: inline-flex; align-items: center; max-width: 220px; gap: 5px; padding: 3px 8px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 12px; }
.agent-file-chip > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-file-chip button { display: grid; place-items: center; padding: 0; border: 0; color: inherit; background: transparent; cursor: pointer; }
.agent-composer__hint { color: var(--td-text-color-secondary); font-size: 12px; }
.agent-send { display: grid; place-items: center; width: 38px; height: 38px; padding: 0; border: 0; border-radius: 50%; color: #fff; background: var(--td-brand-color); cursor: pointer; }
.agent-send.disabled { background: var(--td-brand-color-disabled); cursor: not-allowed; }
.agent-send svg { width: 21px; height: 21px; }
.agent-stop { display: grid; place-items: center; width: 38px; height: 38px; padding: 0; border: 0; border-radius: 50%; color: var(--td-text-color-primary); background: var(--td-bg-color-secondarycontainer); cursor: pointer; }
.agent-stop:hover { background: var(--td-bg-color-container-hover); }
.agent-stop svg { width: 21px; height: 21px; }
.agent-disclaimer { margin: 10px 0 0; color: var(--td-text-color-placeholder); font-size: 12px; }
.agent-source-preview { display: flex; height: min(76vh, 820px); min-height: 480px; flex-direction: column; gap: 10px; }
.agent-source-preview main { min-height: 0; flex: 1; }
.agent-source-preview footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 10px; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-secondary); font-size: 12px; }
.agent-source-preview__actions { display: inline-flex; align-items: center; gap: 8px; }
.agent-source-preview-dialog .t-dialog__body { padding: 0; }
@media (max-width: 760px) { .agent-chat-page { min-height: 560px; } .agent-chat-scroll { padding: 24px 16px 185px; } .agent-scroll-latest { bottom: 154px; } .agent-welcome { padding-bottom: 80px; } .agent-welcome h1 { font-size: 28px; } .agent-message { margin-bottom: 30px; } .agent-response { gap: 10px; } .agent-response__mark { width: 26px; height: 26px; flex-basis: 26px; } .agent-answer { width: 100%; margin-left: 0; } .agent-source-card { display: grid; } .agent-source-card__actions { justify-content: flex-start; } .agent-composer-area { padding: 0 12px 14px; } .agent-textarea :deep(.t-textarea__inner) { min-height: 100px; padding: 13px 14px 58px; font-size: 14px; } .agent-input-request { grid-template-columns: 1fr; } .agent-input-request button { grid-column: 1; } }
@media (hover: none) { .agent-user-actions, .agent-answer__actions { opacity: 1; pointer-events: auto; } }
</style>
