<template>
  <section class="knowledge-structure-page">
    <t-alert
      class="structure-mock-banner"
      theme="warning"
      title="设计稿演示"
      message="本页数据均为本地假数据，尚未接入 RAG 接口，不代表已实现功能，请勿据此验收。"
      :close="false"
    />
    <header class="structure-heading">
      <button class="structure-heading__back" type="button" aria-label="返回我的组织" @click="router.push('/organization')">
        <t-icon name="chevron-left" />
      </button>
      <div class="structure-heading__copy">
        <p>Knowledge structure</p>
        <h1>知识结构</h1>
        <span>查看实体、主题和候选节点的组织方式，并验证树形检索分支。</span>
      </div>
      <t-button variant="outline" @click="resetDemo">
        <template #icon><t-icon name="refresh" /></template>
        重置演示数据
      </t-button>
    </header>

    <section class="structure-summary" aria-label="知识结构概览">
      <div v-for="item in summary" :key="item.label">
        <span>{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
      </div>
    </section>

    <div class="structure-tabs" role="tablist" aria-label="知识结构视图">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        role="tab"
        :aria-selected="activeTab === tab.key"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        <t-icon :name="tab.icon" />
        <span>{{ tab.label }}</span>
        <em v-if="tab.count">{{ tab.count }}</em>
      </button>
    </div>

    <template v-if="activeTab === 'nodes'">
      <section class="structure-workspace">
        <aside class="structure-tree" aria-label="知识节点树">
          <div class="structure-panel-heading">
            <div>
              <strong>节点结构</strong>
              <span>组织范围内的实体与主题</span>
            </div>
            <div class="tree-panel-actions">
              <button type="button" @click="expandAllNodes">全部展开</button>
              <button type="button" @click="collapseAllNodes">全部收起</button>
            </div>
          </div>
          <div class="structure-tree__search">
            <t-input v-model="nodeQuery" clearable placeholder="搜索节点">
              <template #prefix-icon><t-icon name="search" /></template>
            </t-input>
          </div>
          <div class="structure-tree__list">
            <button
              v-for="node in visibleTreeNodes"
              :key="node.id"
              type="button"
              class="structure-tree__row"
              :class="{ selected: selectedNodeId === node.id }"
              :style="{ paddingLeft: `${12 + node.depth * 20}px` }"
              @click="selectNode(node.id)"
            >
              <span v-if="hasChildren(node)" class="structure-tree__toggle" @click.stop="toggleNode(node.id)">
                <t-icon :name="expandedNodeIds.has(node.id) ? 'chevron-down' : 'chevron-right'" />
              </span>
              <span v-else class="structure-tree__toggle structure-tree__toggle--empty" />
              <t-icon :name="nodeIcon(node)" />
              <span class="structure-tree__label">{{ node.label }}</span>
              <t-tag v-if="node.status" :theme="nodeTagTheme(node.status)" variant="light">{{ statusLabel(node.status) }}</t-tag>
            </button>
          </div>
        </aside>

        <article v-if="selectedNode" class="structure-detail">
          <div class="structure-detail__header">
            <div>
              <span class="structure-detail__eyebrow">{{ selectedNode.typeLabel }}</span>
              <h2>{{ selectedNode.label }}</h2>
              <p>{{ selectedNode.description }}</p>
            </div>
            <t-tag :theme="nodeTagTheme(selectedNode.status)" variant="light">{{ statusLabel(selectedNode.status) }}</t-tag>
          </div>

          <dl class="structure-facts">
            <div><dt>标准类型</dt><dd>{{ selectedNode.domain }}</dd></div>
            <div><dt>置信度</dt><dd>{{ selectedNode.confidence ? `${Math.round(selectedNode.confidence * 100)}%` : '人工维护' }}</dd></div>
            <div><dt>关联 Chunk</dt><dd>{{ selectedNode.chunkCount }}</dd></div>
            <div><dt>来源文件</dt><dd>{{ selectedNode.sourceCount }}</dd></div>
          </dl>

          <section class="structure-detail__section">
            <div class="section-title"><strong>别名</strong><span>用于查询时的实体归一化</span></div>
            <div class="alias-list">
              <t-tag v-for="alias in selectedNode.aliases" :key="alias" variant="outline">{{ alias }}</t-tag>
              <span v-if="!selectedNode.aliases.length" class="muted">暂无别名</span>
            </div>
          </section>

          <section class="structure-detail__section">
            <div class="section-title"><strong>分支键</strong><span>树检索使用的 branch key</span></div>
            <div class="branch-list">
              <code v-for="key in selectedNode.branchKeys" :key="key">{{ key }}</code>
            </div>
          </section>

          <section class="structure-detail__section">
            <div class="section-title"><strong>证据来源</strong><span>LLM 与规则提取时保留的原文</span></div>
            <div class="evidence-list">
              <blockquote v-for="evidence in selectedNode.evidence" :key="evidence.quote">
                <p>{{ evidence.quote }}</p>
                <footer>{{ evidence.source }} · chunk {{ evidence.chunkIndex }}</footer>
              </blockquote>
            </div>
          </section>

          <div class="structure-detail__actions">
            <t-button theme="primary" @click="updateSelectedNode('active')">确认正式</t-button>
            <t-button variant="outline" @click="updateSelectedNode('provisional')">设为试用</t-button>
            <t-button variant="outline" @click="mockAction('已提交分支重建任务')">重建分支</t-button>
            <t-button theme="danger" variant="text" @click="updateSelectedNode('disabled')">停用节点</t-button>
          </div>
        </article>
      </section>
    </template>

    <template v-else-if="activeTab === 'candidates'">
      <section class="candidate-section">
        <div class="candidate-toolbar">
          <div>
            <strong>候选节点</strong>
            <span>LLM 提取后先进入候选区，通过规则校验后可成为试用节点</span>
          </div>
          <div class="candidate-toolbar__filters">
            <t-input v-model="candidateQuery" clearable placeholder="搜索候选名称">
              <template #prefix-icon><t-icon name="search" /></template>
            </t-input>
            <select v-model="candidateStatus" class="filter-select" aria-label="候选状态筛选">
              <option value="all">全部状态</option>
              <option value="llm_accepted">LLM 已通过</option>
              <option value="pending">待确认</option>
              <option value="low_quality">低质量</option>
            </select>
          </div>
        </div>

        <div class="candidate-table" role="table" aria-label="候选节点列表">
          <div class="candidate-row candidate-row--head" role="row">
            <span>候选名称</span>
            <span>类型</span>
            <span>提取方式</span>
            <span>状态</span>
            <span>置信度</span>
            <span>证据</span>
            <span aria-hidden="true"></span>
          </div>
          <button
            v-for="candidate in filteredCandidates"
            :key="candidate.id"
            type="button"
            class="candidate-row"
            role="row"
            @click="openCandidate(candidate.id)"
          >
            <span class="candidate-name"><strong>{{ candidate.name }}</strong><small>{{ candidate.source }}</small></span>
            <span><t-tag variant="outline">{{ candidate.type }}</t-tag></span>
            <span class="candidate-method">{{ candidate.method }}</span>
            <span><t-tag :theme="candidateTagTheme(candidate.status)" variant="light">{{ candidateStatusLabel(candidate.status) }}</t-tag></span>
            <span class="candidate-confidence">{{ Math.round(candidate.confidence * 100) }}%</span>
            <span class="candidate-evidence">{{ candidate.evidence.length }} 条</span>
            <span class="candidate-open">审查 <t-icon name="chevron-right" /></span>
          </button>
          <div v-if="!filteredCandidates.length" class="candidate-empty">
            <t-icon name="search" />
            <strong>没有匹配的候选节点</strong>
            <span>调整搜索词或状态筛选后重试</span>
          </div>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="structure-test">
        <div class="structure-test__form">
          <div class="structure-test__heading">
            <div>
              <strong>分支命中测试</strong>
              <span>对比传统检索和树增强检索的候选结果</span>
            </div>
            <t-tag theme="warning" variant="light">Mock 数据</t-tag>
          </div>
          <label>
            <span>测试问题</span>
            <t-input v-model="testQuery" placeholder="输入一个可以定位实体和主题的问题" />
          </label>
          <div class="mode-switch" role="radiogroup" aria-label="检索模式">
            <button v-for="mode in testModes" :key="mode.key" type="button" :class="{ active: testMode === mode.key }" @click="testMode = mode.key">
              {{ mode.label }}
            </button>
          </div>
          <t-button theme="primary" block :loading="testLoading" @click="runTreeTest">
            <template #icon><t-icon name="search" /></template>
            运行测试
          </t-button>
        </div>

        <div class="structure-test__result">
          <div class="test-result-summary">
            <div><span>解析实体</span><strong>{{ testResult.entity }}</strong></div>
            <div><span>解析主题</span><strong>{{ testResult.topic }}</strong></div>
            <div><span>命中分支</span><strong>{{ testResult.branchKeys.length }}</strong></div>
          </div>
          <div class="test-branches">
            <code v-for="key in testResult.branchKeys" :key="key">{{ key }}</code>
          </div>
          <div class="test-comparison">
            <div class="test-comparison__head"><span>检索方式</span><span>答案 Chunk 排名</span><span>送入上下文</span><span>耗时</span></div>
            <div v-for="row in testResult.rows" :key="row.name" class="test-comparison__row">
              <strong>{{ row.name }}</strong>
              <span>{{ row.rank }}</span>
              <span>{{ row.context }}</span>
              <span>{{ row.duration }}</span>
            </div>
          </div>
        </div>
      </section>
    </template>

    <t-drawer v-model:visible="candidateDrawerVisible" header="候选节点详情" size="480px" :footer="false" destroy-on-close>
      <div v-if="selectedCandidate" class="candidate-drawer">
        <div class="candidate-drawer__hero">
          <span class="candidate-drawer__icon"><t-icon name="node-tree" /></span>
          <div>
            <h3>{{ selectedCandidate.name }}</h3>
            <p>{{ selectedCandidate.type }} · {{ selectedCandidate.method }}</p>
          </div>
          <t-tag :theme="candidateTagTheme(selectedCandidate.status)" variant="light">{{ candidateStatusLabel(selectedCandidate.status) }}</t-tag>
        </div>

        <div class="candidate-drawer__score">
          <div><span>模型置信度</span><strong>{{ Math.round(selectedCandidate.confidence * 100) }}%</strong></div>
          <div><span>提及次数</span><strong>{{ selectedCandidate.mentions }}</strong></div>
          <div><span>证据 Chunk</span><strong>{{ selectedCandidate.evidence.length }}</strong></div>
        </div>

        <section class="drawer-section">
          <strong>LLM 判断理由</strong>
          <p>{{ selectedCandidate.reason }}</p>
        </section>

        <section class="drawer-section">
          <strong>建议别名</strong>
          <div class="alias-list">
            <t-tag v-for="alias in selectedCandidate.aliases" :key="alias" variant="outline">{{ alias }}</t-tag>
            <span v-if="!selectedCandidate.aliases.length" class="muted">暂无别名</span>
          </div>
        </section>

        <section class="drawer-section">
          <strong>原文证据</strong>
          <blockquote v-for="evidence in selectedCandidate.evidence" :key="evidence.quote">
            <p>{{ evidence.quote }}</p>
            <footer>{{ evidence.source }} · chunk {{ evidence.chunkIndex }}</footer>
          </blockquote>
        </section>

        <section class="drawer-section">
          <strong>晋级后影响</strong>
          <div class="impact-list">
            <span>{{ selectedCandidate.mentions }} 个 Chunk 将重新匹配</span>
            <span>预计生成 {{ Math.max(1, selectedCandidate.aliases.length + 1) }} 个 branch key</span>
            <span>分支在试用节点下先参与 shadow 检索</span>
          </div>
        </section>

        <div class="candidate-drawer__actions">
          <t-button theme="primary" @click="reviewCandidate('provisional')">加入试用节点</t-button>
          <t-button variant="outline" @click="reviewCandidate('active')">确认正式</t-button>
          <t-button variant="outline" @click="mockAction('已打开实体合并选择器')">合并到已有实体</t-button>
          <t-button theme="danger" variant="text" @click="reviewCandidate('ignored')">忽略</t-button>
        </div>
      </div>
    </t-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useRouter } from 'vue-router'

type NodeStatus = 'provisional' | 'active' | 'pending' | 'disabled'
type CandidateStatus = 'llm_accepted' | 'pending' | 'low_quality' | 'provisional' | 'active' | 'ignored'
type Evidence = { source: string; chunkIndex: number; quote: string }
type TreeNode = {
  id: string
  parentId: string | null
  label: string
  typeLabel: string
  depth: number
  icon: string
  domain: string
  description: string
  status: NodeStatus
  confidence?: number
  chunkCount: number
  sourceCount: number
  count?: number
  aliases: string[]
  branchKeys: string[]
  evidence: Evidence[]
}
type Candidate = {
  id: string
  name: string
  type: string
  method: string
  source: string
  status: CandidateStatus
  confidence: number
  mentions: number
  aliases: string[]
  reason: string
  evidence: Evidence[]
}

const router = useRouter()
const activeTab = ref<'nodes' | 'candidates' | 'test'>('nodes')
const nodeQuery = ref('')
const candidateQuery = ref('')
const candidateStatus = ref<'all' | CandidateStatus>('all')
const selectedNodeId = ref('entity-qingyun')
const expandedNodeIds = ref(new Set(['organization-root', 'domain-organization', 'entity-qingyun']))
const candidateDrawerVisible = ref(false)
const selectedCandidateId = ref('')
const testQuery = ref('青云小组的作息是什么')
const testMode = ref<'traditional' | 'tree' | 'compare'>('compare')
const testLoading = ref(false)

const tabs = computed(() => [
  { key: 'nodes' as const, label: '节点结构', icon: 'node-tree', count: treeNodes.value.length },
  { key: 'candidates' as const, label: '候选审查', icon: 'user-check', count: candidates.value.filter((item) => item.status === 'llm_accepted' || item.status === 'pending').length },
  { key: 'test' as const, label: '检索测试', icon: 'chart-bubble', count: 0 },
])

const summary = computed(() => [
  { label: '正式节点', value: treeNodes.value.filter((item) => item.status === 'active').length },
  { label: '试用节点', value: treeNodes.value.filter((item) => item.status === 'provisional').length },
  { label: '候选节点', value: candidates.value.filter((item) => item.status !== 'ignored' && item.status !== 'active').length },
  { label: '分支覆盖率', value: '82%' },
])

const treeNodes = ref<TreeNode[]>([
  {
    id: 'organization-root', parentId: null, label: '狗熊岭', typeLabel: '组织范围', depth: 0, icon: 'usergroup', domain: 'organization',
    description: '当前组织下所有已确认实体和主题节点。', status: 'active', chunkCount: 0, sourceCount: 0, count: 5,
    aliases: [], branchKeys: ['scope:organization:dog-bear'], evidence: [],
  },
  {
    id: 'domain-organization', parentId: 'organization-root', label: '组织实体', typeLabel: '领域', depth: 1, icon: 'folder', domain: 'organization',
    description: '组织、团队、部门和社团类实体。', status: 'active', chunkCount: 0, sourceCount: 0, count: 2,
    aliases: [], branchKeys: ['domain:organization'], evidence: [],
  },
  {
    id: 'entity-qingyun', parentId: 'domain-organization', label: '青云飞鹏小组', typeLabel: '实体节点', depth: 2, icon: 'usergroup', domain: 'organization',
    description: '学术科技类社团，当前作为树形检索的试用实体。', status: 'provisional', confidence: 0.93,
    chunkCount: 18, sourceCount: 3, count: 4, aliases: ['青云小组', '青云飞鹏'],
    branchKeys: ['entity:organization:qingyun', 'entity:organization:qingyun:topic:作息'],
    evidence: [
      { source: '常见问题.docx', chunkIndex: 2, quote: '青云飞鹏小组属于学术科技类社团，专注于计算机技术学习与项目实践。' },
      { source: '飞书 / aims', chunkIndex: 0, quote: '青云飞鹏小组马上招新了，官网进度要抓紧了。' },
      { source: '常见问题.docx', chunkIndex: 16, quote: '青云飞鹏小组作息安排如图（1）所示。' },
    ],
  },
  {
    id: 'topic-recruit', parentId: 'entity-qingyun', label: '招新', typeLabel: '主题节点', depth: 3, icon: 'user-add', domain: 'topic',
    description: '招新对象、面试方式、培养体系与报名入口。', status: 'provisional', confidence: 0.91,
    chunkCount: 4, sourceCount: 1, aliases: [], branchKeys: ['entity:organization:qingyun:topic:招新'],
    evidence: [{ source: '常见问题.docx', chunkIndex: 4, quote: '青云飞鹏小组招新主要面向大一同学。' }],
  },
  {
    id: 'topic-schedule', parentId: 'entity-qingyun', label: '作息安排', typeLabel: '主题节点', depth: 3, icon: 'time', domain: 'topic',
    description: '冬季、夏季作息和固定学习时间安排。', status: 'provisional', confidence: 0.95,
    chunkCount: 4, sourceCount: 1, aliases: [], branchKeys: ['entity:organization:qingyun:topic:作息'],
    evidence: [{ source: '常见问题.docx', chunkIndex: 16, quote: '作息时间区分冬季、夏季：上午时段冬夏一致，为 8:30 至 11:40。' }],
  },
  {
    id: 'topic-training', parentId: 'entity-qingyun', label: '培养体系', typeLabel: '主题节点', depth: 3, icon: 'education', domain: 'topic',
    description: '组长带领、定期考核和项目实践。', status: 'active', confidence: 0.88,
    chunkCount: 6, sourceCount: 2, aliases: ['学习机制'], branchKeys: ['entity:organization:qingyun:topic:培养体系'],
    evidence: [{ source: '常见问题.docx', chunkIndex: 12, quote: '具有成体系且稳定可靠的培养路线。' }],
  },
])

const candidates = ref<Candidate[]>([
  {
    id: 'candidate-qingyun', name: '青云飞鹏小组', type: '组织', method: 'LLM + 规则', source: '常见问题.docx + 飞书 / aims',
    status: 'llm_accepted', confidence: 0.93, mentions: 3, aliases: ['青云小组'],
    reason: '该名称在多个来源中稳定出现，别名关系明确，且上下文描述为学术科技类社团。',
    evidence: [
      { source: '常见问题.docx', chunkIndex: 2, quote: '青云飞鹏小组属于学术科技类社团。' },
      { source: '飞书 / aims', chunkIndex: 0, quote: '青云飞鹏小组马上招新了。' },
    ],
  },
  {
    id: 'candidate-schedule', name: '作息安排', type: '主题', method: 'LLM + 标题规则', source: '常见问题.docx',
    status: 'llm_accepted', confidence: 0.95, mentions: 2, aliases: ['作息时间'],
    reason: '问答标题和正文都围绕固定学习时间、冬夏作息展开，属于稳定的主题节点。',
    evidence: [{ source: '常见问题.docx', chunkIndex: 16, quote: '作息时间区分冬季、夏季。' }],
  },
  {
    id: 'candidate-system', name: '通过系统', type: '项目', method: '正则', source: '常见问题.docx',
    status: 'low_quality', confidence: 0.42, mentions: 1, aliases: [],
    reason: '该片段缺少稳定指代，更像是句子中的动词短语，不符合实体命名规则。',
    evidence: [{ source: '常见问题.docx', chunkIndex: 2, quote: '通过系统的学习和互助交流。' }],
  },
  {
    id: 'candidate-recruit', name: '招新', type: '主题', method: 'LLM', source: '常见问题.docx',
    status: 'pending', confidence: 0.91, mentions: 4, aliases: ['招新模式'],
    reason: '问题与答案集中描述招新对象、面试和培养方式，与实体“青云飞鹏小组”形成稳定的主题关联。',
    evidence: [{ source: '常见问题.docx', chunkIndex: 4, quote: '招新主要面向大一同学。' }],
  },
])

const selectedNode = computed(() => treeNodes.value.find((item) => item.id === selectedNodeId.value))
const selectedCandidate = computed(() => candidates.value.find((item) => item.id === selectedCandidateId.value))
const visibleTreeNodes = computed(() => {
  const needle = nodeQuery.value.trim().toLowerCase()
  if (needle) return treeNodes.value.filter((item) => item.label.toLowerCase().includes(needle))
  const byId = new Map(treeNodes.value.map((item) => [item.id, item]))
  return treeNodes.value.filter((item) => {
    let parentId = item.parentId
    while (parentId) {
      if (!expandedNodeIds.value.has(parentId)) return false
      parentId = byId.get(parentId)?.parentId || null
    }
    return true
  })
})
const filteredCandidates = computed(() => {
  const needle = candidateQuery.value.trim().toLowerCase()
  return candidates.value.filter((item) => {
    const matchesText = !needle || `${item.name} ${item.source} ${item.type}`.toLowerCase().includes(needle)
    const matchesStatus = candidateStatus.value === 'all' || item.status === candidateStatus.value
    return matchesText && matchesStatus
  })
})

const testModes = [
  { key: 'traditional' as const, label: '传统检索' },
  { key: 'tree' as const, label: '树增强' },
  { key: 'compare' as const, label: '对比模式' },
]
const testResult = ref({
  entity: '青云飞鹏小组',
  topic: '作息安排',
  branchKeys: [
    'entity:organization:qingyun',
    'entity:organization:qingyun:topic:作息',
  ],
  rows: [
    { name: '传统检索', rank: '4', context: 'chunk 14、15、16、17', duration: '1.24s' },
    { name: '树增强', rank: '1', context: 'chunk 15、16', duration: '1.31s' },
  ],
})

function nodeIcon(node: TreeNode) { return node.icon }
function hasChildren(node: TreeNode) { return treeNodes.value.some((item) => item.parentId === node.id) }
function toggleNode(id: string) {
  const next = new Set(expandedNodeIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedNodeIds.value = next
}
function expandAllNodes() {
  expandedNodeIds.value = new Set(treeNodes.value.filter((item) => hasChildren(item)).map((item) => item.id))
}
function collapseAllNodes() {
  expandedNodeIds.value = new Set(treeNodes.value.filter((item) => !item.parentId).map((item) => item.id))
}
function statusLabel(status: NodeStatus) {
  return { provisional: '试用节点', active: '正式节点', pending: '待确认', disabled: '已停用' }[status]
}
function nodeTagTheme(status: NodeStatus): 'success' | 'warning' | 'primary' | 'default' {
  return status === 'active' ? 'success' : status === 'provisional' ? 'warning' : status === 'pending' ? 'primary' : 'default'
}
function candidateStatusLabel(status: CandidateStatus) {
  return {
    llm_accepted: 'LLM 已通过',
    pending: '待确认',
    low_quality: '低质量',
    provisional: '试用节点',
    active: '正式节点',
    ignored: '已忽略',
  }[status]
}
function candidateTagTheme(status: CandidateStatus): 'success' | 'warning' | 'primary' | 'danger' | 'default' {
  return status === 'active' || status === 'provisional' ? 'success' : status === 'pending' ? 'primary' : status === 'low_quality' ? 'danger' : status === 'ignored' ? 'default' : 'warning'
}
function selectNode(id: string) { selectedNodeId.value = id }
function updateSelectedNode(status: NodeStatus) {
  const node = selectedNode.value
  if (!node) return
  node.status = status
  MessagePlugin.success(status === 'active' ? '节点已确认正式，演示分支已刷新' : status === 'provisional' ? '节点已设为试用' : '节点已停用')
}
function openCandidate(id: string) { selectedCandidateId.value = id; candidateDrawerVisible.value = true }
function reviewCandidate(status: CandidateStatus) {
  const candidate = selectedCandidate.value
  if (!candidate) return
  candidate.status = status
  candidateDrawerVisible.value = false
  MessagePlugin.success(status === 'provisional' ? '已加入试用节点，等待分支刷新' : status === 'active' ? '已确认正式节点' : '候选已忽略')
}
function mockAction(message: string) { MessagePlugin.success(message) }
function resetDemo() {
  activeTab.value = 'nodes'
  nodeQuery.value = ''
  candidateQuery.value = ''
  candidateStatus.value = 'all'
  MessagePlugin.success('演示数据已重置')
}
async function runTreeTest() {
  if (!testQuery.value.trim() || testLoading.value) return
  testLoading.value = true
  await new Promise((resolve) => window.setTimeout(resolve, 600))
  testResult.value = {
    entity: testQuery.value.includes('青云') ? '青云飞鹏小组' : '未识别实体',
    topic: testQuery.value.includes('作息') ? '作息安排' : testQuery.value.includes('招新') ? '招新' : '未识别主题',
    branchKeys: testQuery.value.includes('青云')
      ? ['entity:organization:qingyun', `entity:organization:qingyun:topic:${testQuery.value.includes('作息') ? '作息' : '招新'}`]
      : ['global:fallback'],
    rows: [
      { name: '传统检索', rank: '4', context: 'chunk 14、15、16、17', duration: '1.24s' },
      { name: '树增强', rank: '1', context: 'chunk 15、16', duration: '1.31s' },
    ],
  }
  testLoading.value = false
}
</script>

<style scoped lang="less">
.knowledge-structure-page { box-sizing: border-box; width: min(1180px, 100%); margin: 0 auto; padding: 30px 34px 58px; color: var(--td-text-color-primary); }
.structure-mock-banner { margin-bottom: 16px; }
.structure-heading { display: flex; align-items: flex-start; gap: 13px; margin-bottom: 20px; }
.structure-heading__back { display: grid; place-items: center; flex: 0 0 34px; width: 34px; height: 34px; margin-top: 2px; padding: 0; border: 1px solid var(--td-component-stroke); border-radius: 7px; color: var(--td-text-color-secondary); background: var(--td-bg-color-container); cursor: pointer; }
.structure-heading__back:hover { color: var(--td-brand-color); border-color: var(--td-brand-color-4); }
.structure-heading__copy { flex: 1; min-width: 0; }
.structure-heading__copy p { margin: 0 0 5px; color: var(--td-brand-color); font-size: 11px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }
.structure-heading__copy h1 { margin: 0; font-size: 28px; font-weight: 650; }
.structure-heading__copy span { display: block; margin-top: 7px; color: var(--td-text-color-secondary); font-size: 13px; }
.structure-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.structure-summary > div { display: grid; min-height: 80px; align-content: center; gap: 8px; padding: 15px 20px; border-left: 1px solid var(--td-component-stroke); }
.structure-summary > div:first-child { border-left: 0; }
.structure-summary span { color: var(--td-text-color-secondary); font-size: 12px; }
.structure-summary strong { font-size: 28px; font-weight: 650; line-height: 1.1; }
.structure-tabs { display: flex; align-items: center; gap: 3px; margin: 20px 0 14px; border-bottom: 1px solid var(--td-component-stroke); }
.structure-tabs button { display: inline-flex; position: relative; align-items: center; gap: 7px; min-height: 38px; padding: 0 13px; border: 0; color: var(--td-text-color-secondary); background: transparent; font: inherit; font-size: 13px; cursor: pointer; }
.structure-tabs button::after { position: absolute; right: 10px; bottom: -1px; left: 10px; height: 2px; border-radius: 2px; background: transparent; content: ''; }
.structure-tabs button.active { color: var(--td-brand-color); font-weight: 600; }
.structure-tabs button.active::after { background: var(--td-brand-color); }
.structure-tabs em { min-width: 18px; padding: 1px 5px; border-radius: 9px; color: var(--td-brand-color-7); background: var(--td-brand-color-1); font-size: 10px; font-style: normal; text-align: center; }
.structure-workspace { display: grid; grid-template-columns: 360px minmax(0, 1fr); min-height: 610px; overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.structure-tree { min-width: 0; border-right: 1px solid var(--td-component-stroke); background: #fbfcfc; }
.structure-panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 15px 16px 13px; border-bottom: 1px solid var(--td-component-stroke); }
.structure-panel-heading > div { display: grid; gap: 3px; min-width: 0; }
.structure-panel-heading strong { font-size: 13px; }
.structure-panel-heading span { color: var(--td-text-color-secondary); font-size: 10px; }
.tree-panel-actions { display: flex; align-items: center; gap: 8px; }
.tree-panel-actions button { padding: 0; border: 0; color: var(--td-text-color-placeholder); background: transparent; font: inherit; font-size: 10px; cursor: pointer; }
.tree-panel-actions button:hover { color: var(--td-brand-color); }
.structure-tree__search { padding: 12px 13px; }
.structure-tree__search :deep(.t-input) { width: 100%; }
.structure-tree__list { display: grid; gap: 2px; padding: 0 8px 14px; }
.structure-tree__row { display: grid; grid-template-columns: 14px 18px minmax(0, 1fr) auto; align-items: center; gap: 7px; min-height: 38px; padding: 6px 8px; border: 0; border-radius: 6px; color: var(--td-text-color-primary); background: transparent; text-align: left; font: inherit; cursor: pointer; }
.structure-tree__row:hover { background: var(--td-bg-color-container-hover); }
.structure-tree__row.selected { color: var(--td-brand-color-7); background: var(--td-brand-color-1); }
.structure-tree__row > svg { color: var(--td-brand-color); }
.structure-tree__label { overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.structure-tree__toggle { display: grid; place-items: center; width: 14px; height: 18px; color: var(--td-text-color-placeholder); }
.structure-tree__toggle:hover { color: var(--td-brand-color); }
.structure-tree__toggle--empty { pointer-events: none; }
.structure-detail { min-width: 0; padding: 22px 24px 24px; }
.structure-detail__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding-bottom: 17px; border-bottom: 1px solid var(--td-component-stroke); }
.structure-detail__eyebrow { color: var(--td-brand-color); font-size: 10px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
.structure-detail h2 { margin: 7px 0 0; font-size: 22px; font-weight: 650; }
.structure-detail__header p { margin: 7px 0 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.65; }
.structure-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0; padding: 18px 0; border-bottom: 1px solid var(--td-component-stroke); }
.structure-facts div { padding: 0 16px; border-left: 1px solid var(--td-component-stroke); }
.structure-facts div:first-child { padding-left: 0; border-left: 0; }
.structure-facts dt { color: var(--td-text-color-secondary); font-size: 10px; }
.structure-facts dd { margin: 5px 0 0; font-size: 13px; font-weight: 600; }
.structure-detail__section { display: grid; gap: 9px; margin-top: 19px; }
.section-title { display: flex; align-items: baseline; gap: 8px; }
.section-title strong { font-size: 12px; }
.section-title span { color: var(--td-text-color-placeholder); font-size: 10px; }
.alias-list, .branch-list { display: flex; flex-wrap: wrap; gap: 7px; }
.branch-list code, .test-branches code { padding: 5px 7px; border: 1px solid var(--td-component-stroke); border-radius: 5px; color: #435267; background: var(--td-bg-color-secondarycontainer); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 10px; }
.evidence-list { display: grid; gap: 8px; }
.evidence-list blockquote, .candidate-drawer blockquote { margin: 0; padding: 10px 12px; border-left: 3px solid var(--td-brand-color-4); border-radius: 0 6px 6px 0; background: var(--td-bg-color-secondarycontainer); }
.evidence-list blockquote p, .candidate-drawer blockquote p { margin: 0; font-size: 12px; line-height: 1.65; }
.evidence-list blockquote footer, .candidate-drawer blockquote footer { margin-top: 6px; color: var(--td-text-color-placeholder); font-size: 10px; }
.structure-detail__actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--td-component-stroke); }
.candidate-section { overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.candidate-toolbar { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; padding: 15px 16px; border-bottom: 1px solid var(--td-component-stroke); }
.candidate-toolbar > div:first-child { display: grid; gap: 4px; }
.candidate-toolbar strong { font-size: 14px; }
.candidate-toolbar span { color: var(--td-text-color-secondary); font-size: 11px; }
.candidate-toolbar__filters { display: flex; align-items: center; gap: 8px; }
.candidate-toolbar__filters .t-input { width: 220px; }
.filter-select { width: 130px; height: 32px; padding: 0 28px 0 10px; border: 1px solid var(--td-component-stroke); border-radius: 4px; color: var(--td-text-color-primary); background: var(--td-bg-color-container); font: inherit; font-size: 12px; }
.candidate-row { display: grid; grid-template-columns: minmax(180px, 1.35fr) 90px 110px 110px 76px 62px 74px; align-items: center; width: 100%; min-height: 66px; gap: 12px; padding: 10px 15px; border: 0; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-primary); background: transparent; text-align: left; font: inherit; cursor: pointer; }
.candidate-row:not(.candidate-row--head):hover { background: var(--td-bg-color-container-hover); }
.candidate-row--head { min-height: 40px; border-top: 0; color: var(--td-text-color-placeholder); background: var(--td-bg-color-secondarycontainer); font-size: 10px; cursor: default; }
.candidate-name { display: grid; min-width: 0; gap: 4px; }
.candidate-name strong, .candidate-name small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.candidate-name strong { font-size: 13px; font-weight: 600; }
.candidate-name small, .candidate-method, .candidate-evidence { color: var(--td-text-color-secondary); font-size: 10px; }
.candidate-confidence { font-size: 12px; font-weight: 600; }
.candidate-open { display: inline-flex; align-items: center; justify-content: flex-end; gap: 3px; color: var(--td-brand-color); font-size: 10px; }
.candidate-empty { display: grid; min-height: 240px; place-items: center; align-content: center; gap: 7px; color: var(--td-text-color-placeholder); }
.candidate-empty strong { color: var(--td-text-color-secondary); font-size: 13px; }
.candidate-empty span { font-size: 10px; }
.structure-test { display: grid; grid-template-columns: 330px minmax(0, 1fr); min-height: 520px; overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 8px; background: var(--td-bg-color-container); }
.structure-test__form { display: grid; align-content: start; gap: 18px; padding: 20px; border-right: 1px solid var(--td-component-stroke); background: #fbfcfc; }
.structure-test__heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.structure-test__heading > div { display: grid; gap: 4px; }
.structure-test__heading strong { font-size: 14px; }
.structure-test__heading span { color: var(--td-text-color-secondary); font-size: 11px; line-height: 1.55; }
.structure-test__form label { display: grid; gap: 8px; color: var(--td-text-color-secondary); font-size: 11px; }
.mode-switch { display: grid; grid-template-columns: repeat(3, 1fr); padding: 3px; border: 1px solid var(--td-component-stroke); border-radius: 7px; background: var(--td-bg-color-secondarycontainer); }
.mode-switch button { min-height: 30px; padding: 0 6px; border: 0; border-radius: 5px; color: var(--td-text-color-secondary); background: transparent; font: inherit; font-size: 11px; cursor: pointer; }
.mode-switch button.active { color: var(--td-brand-color-7); background: var(--td-bg-color-container); box-shadow: 0 1px 3px rgb(0 0 0 / 8%); }
.structure-test__result { display: grid; align-content: start; gap: 18px; padding: 20px 22px; }
.test-result-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 7px; }
.test-result-summary > div { display: grid; gap: 4px; padding: 12px 14px; border-left: 1px solid var(--td-component-stroke); }
.test-result-summary > div:first-child { border-left: 0; }
.test-result-summary span { color: var(--td-text-color-secondary); font-size: 10px; }
.test-result-summary strong { font-size: 13px; }
.test-branches { display: flex; flex-wrap: wrap; gap: 7px; }
.test-comparison { overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 7px; }
.test-comparison__head, .test-comparison__row { display: grid; grid-template-columns: 120px 110px minmax(180px, 1fr) 70px; align-items: center; gap: 10px; padding: 10px 13px; }
.test-comparison__head { color: var(--td-text-color-placeholder); background: var(--td-bg-color-secondarycontainer); font-size: 10px; }
.test-comparison__row { min-height: 48px; border-top: 1px solid var(--td-component-stroke); color: var(--td-text-color-secondary); font-size: 11px; }
.test-comparison__row strong { color: var(--td-text-color-primary); font-size: 12px; }
.candidate-drawer { display: grid; gap: 19px; }
.candidate-drawer__hero { display: flex; align-items: center; gap: 12px; }
.candidate-drawer__icon { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 8px; color: #fff; background: var(--td-brand-color); }
.candidate-drawer__hero > div { flex: 1; min-width: 0; }
.candidate-drawer__hero h3 { margin: 0; font-size: 17px; }
.candidate-drawer__hero p { margin: 5px 0 0; color: var(--td-text-color-secondary); font-size: 11px; }
.candidate-drawer__score { display: grid; grid-template-columns: repeat(3, 1fr); overflow: hidden; border: 1px solid var(--td-component-stroke); border-radius: 7px; }
.candidate-drawer__score div { display: grid; gap: 4px; padding: 11px; border-left: 1px solid var(--td-component-stroke); }
.candidate-drawer__score div:first-child { border-left: 0; }
.candidate-drawer__score span { color: var(--td-text-color-secondary); font-size: 10px; }
.candidate-drawer__score strong { font-size: 14px; }
.drawer-section { display: grid; gap: 9px; }
.drawer-section > strong { font-size: 12px; }
.drawer-section > p { margin: 0; color: var(--td-text-color-secondary); font-size: 12px; line-height: 1.7; }
.impact-list { display: grid; gap: 7px; padding: 11px 12px; border-radius: 6px; color: var(--td-text-color-secondary); background: var(--td-bg-color-secondarycontainer); font-size: 11px; }
.candidate-drawer__actions { display: flex; flex-wrap: wrap; gap: 8px; padding-top: 17px; border-top: 1px solid var(--td-component-stroke); }
.muted { color: var(--td-text-color-placeholder); font-size: 11px; }
@media (max-width: 900px) {
  .knowledge-structure-page { padding: 22px 16px 45px; }
  .structure-summary { grid-template-columns: repeat(2, 1fr); }
  .structure-summary > div:nth-child(3) { border-left: 0; border-top: 1px solid var(--td-component-stroke); }
  .structure-summary > div:nth-child(4) { border-top: 1px solid var(--td-component-stroke); }
  .structure-workspace, .structure-test { grid-template-columns: 1fr; }
  .structure-tree, .structure-test__form { border-right: 0; border-bottom: 1px solid var(--td-component-stroke); }
  .candidate-toolbar { align-items: stretch; flex-direction: column; }
  .candidate-toolbar__filters { flex-wrap: wrap; }
  .candidate-row { grid-template-columns: minmax(170px, 1fr) 90px 95px 75px; }
  .candidate-row > span:nth-child(3), .candidate-row > span:nth-child(6) { display: none; }
}
</style>
