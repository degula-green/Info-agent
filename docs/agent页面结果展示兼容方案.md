# Agent 页面结果展示兼容方案

## 文档状态

- 版本：v1.1
- 状态：阶段已固定，待实施
- 目标：让 Agent 页面稳定完成“工具检索 -> 生成回答 -> 展示摘要与证据”
- 适用范围：Agent 页面、Agent Kernel、Knowledge Capability、RAG 返回结果
- 历史数据：旧 QA 历史已清空，不需要迁移
- 最后更新：2026-10-01

## 1. 最终目标

Agent 页面成为唯一对话入口：

```text
/chat
├─ 快速问答
├─ Agent 任务
└─ 混合任务
```

用户不需要理解“RAG 问答”和“Agent 任务”的区别。

Agent 内部根据问题选择：

```text
来源定位 -> knowledge.search_sources
内容问答 -> knowledge.search_content -> knowledge.answer
多步任务 -> search_sources -> search_content -> answer
操作任务 -> todo / web / approval
```

最终稳定链路：

```text
用户问题
-> Planner
-> 知识工具
-> 授权后的 sources/chunks
-> knowledge.answer
-> answer + citations
-> Agent 页面 result blocks
```

## 2. 稳定工具调用策略

不能只依赖 LLM 自主选择工具，否则同样的问题可能被规划成不同链路。

采用“确定性路由优先，LLM 补充”：

| 用户表达 | 固定链路 |
|---|---|
| 谁发的、哪个群、什么时间、哪些文件 | `search_sources` |
| 文件是谁发的、消息在哪个群 | `search_sources` |
| 说了什么、内容是什么、怎么规定 | `search_content -> knowledge.answer` |
| 某人发的某文件写了什么 | `search_sources -> search_content -> knowledge.answer` |
| 创建待办、提醒、日程 | 现有 action capability |
| 闲聊、无明确任务 | 直接回答，不调用检索 |

LLM Planner 负责：

- 抽取 sender、conversation、时间和关键词
- 解析“上个月”“最近一周”等时间表达
- 不适合规则路由时决定补充计划

确定性路由负责：

- 避免漏调知识工具
- 避免工具选择漂移
- 避免元数据问题被错误当成内容问答
- 避免简单问答进入多步规划

## 3. knowledge.answer

新增 `knowledge.answer`，负责稳定生成知识回答。

输入：

```json
{
  "query": "合同的违约责任是什么",
  "results": [
    {
      "resource_id": "attachment-1",
      "resource_type": "attachment",
      "title": "采购合同V2.pdf",
      "sender_name": "张三",
      "conversation_name": "法务群",
      "sent_at": "2026-07-15T14:30:00+08:00",
      "chunks": [
        {
          "chunk_id": "chunk-1",
          "text": "第八条 违约责任……",
          "position": {"page_number": 8}
        }
      ]
    }
  ]
}
```

输出：

```json
{
  "answer": "合同第八条约定……",
  "citations": [
    {
      "evidence_id": "chunk-1",
      "resource_id": "attachment-1",
      "title": "采购合同V2.pdf",
      "quote": "第八条 违约责任……",
      "sender_name": "张三",
      "conversation_name": "法务群",
      "sent_at": "2026-07-15T14:30:00+08:00",
      "position": {"page_number": 8}
    }
  ],
  "metadata_coverage": "complete"
}
```

约束：

- citation 只能引用输入 results 中存在的 chunk。
- 无检索结果时禁止模型编造回答。
- protected 原文只能来自 RAG 已授权结果。
- `metadata_coverage=partial` 必须透传到页面。

实现优先级：

1. 首选新增 `knowledge.answer`，避免把知识结果转换逻辑塞进通用 `answer.compose`。
2. 如果复用 `answer.compose`，必须扩展 input binding，允许接收 `knowledge.search_content` 输出。

## 4. Agent 页面结果块

Agent 消息统一为：

```ts
type AgentResultBlock =
  | AnswerBlock
  | SourceListBlock
  | ContentResultBlock
  | TodoBlock
  | ApprovalBlock
  | InputRequestBlock
  | ErrorBlock

type AgentMessage = {
  text?: string
  status?: string
  steps: Step[]
  blocks: AgentResultBlock[]
  lastEventId: number
}
```

### AnswerBlock

```ts
{
  type: 'answer'
  text: string
  citations: Citation[]
}
```

### SourceListBlock

```ts
{
  type: 'source_list'
  summary: string
  items: SourceInfo[]
  resource_ids: string[]
  has_more: boolean
  metadata_coverage: 'complete' | 'partial'
}
```

### ContentResultBlock

```ts
{
  type: 'content_results'
  results: ContentResult[]
  returned_source_count: number
  returned_chunk_count: number
  metadata_coverage: 'complete' | 'partial'
}
```

工具映射：

| 工具输出 | Block |
|---|---|
| `knowledge.search_sources` | `source_list` |
| `knowledge.search_content` | `content_results` |
| `knowledge.answer` | `answer` |
| `todo.create` | `todo` |
| 审批 | `approval` |
| 等待补充信息 | `input_request` |
| 失败 | `error` |

## 5. 摘要、回答和证据展示规则

### 5.1 检索摘要

展示内容示例：

```text
找到 3 个文件，来自 2 个群聊
```

展示条件：

- 调用了 `search_sources`
- 调用了 `search_content`
- 发生了知识检索后回答

不展示条件：

- 纯待办、日程、审批任务
- 闲聊
- 用户只上传文件但没有检索
- 工具在检索前失败

### 5.2 回答正文

展示条件：

- 文件内容问答
- 政策、合同、项目内容分析
- 多来源总结
- 问题需要解释而不是只列资源

不展示条件：

- “谁发过哪些文件”这类元数据问题
- 用户只要列表
- 无检索结果
- 权限不足

### 5.3 证据

必须展示：

- 回答引用了公司知识
- 包含具体数字、金额、日期、条款
- 多个来源可能冲突
- 用户要求依据或原文

不展示：

- 创建待办
- 日程、审批
- 闲聊
- 无外部知识检索
- 纯操作结果

元数据问题的来源列表本身就是结果和证据，不重复生成一段散文回答。

### 5.4 无结果

展示：

```text
没有找到满足条件的内容。
检索范围：当前组织 / 最近 30 天
```

禁止：

- 模型根据常识编造答案
- 展示无关来源
- 把旧数据缺失伪装成“确实不存在”

### 5.5 历史数据 coverage 不足

展示：

```text
部分历史数据尚未完成元数据补齐，本次结果仅覆盖已索引内容。
```

同时标记：

```text
metadata_coverage=partial
```

## 6. 展示时序

```text
task.accepted
-> 显示“正在理解问题”

step.started(search_sources/search_content)
-> 显示“正在检索”

step.succeeded(search_sources)
-> 即时显示检索摘要
-> 可先显示来源列表摘要，不返回完整敏感内容

step.started(knowledge.answer)
-> 显示“正在整理回答”

task.completed
-> 显示回答正文
-> 显示 citations
-> 显示来源和文件预览入口
```

第一阶段可以只在任务完成时补拉 observations。

第二阶段在 `step.succeeded` 增加脱敏 `result_preview`，实现实时摘要：

```json
{
  "block_type": "source_list",
  "summary": "找到3个文件",
  "item_count": 3,
  "metadata_coverage": "complete"
}
```

SSE 不发送：

- protected 原文
- 完整 chunks
- 本地路径
- MinIO object ref

## 7. 页面布局

桌面端：

```text
消息区：最大 900-960px
来源区：右侧 300-360px，可折叠
执行轨迹：默认折叠
审批/输入：主消息流 action card
```

移动端：

```text
回答
-> 来源卡片
-> chunks
-> 操作按钮
```

来源展示不能只放在固定右侧抽屉。移动端必须内联展示。

## 8. 固定改造阶段

### Phase 0：契约和路由冻结

目标：

- 冻结 `AgentResultBlock`
- 冻结工具到 block 的映射
- 冻结确定性路由规则
- 冻结 summary/evidence/no-evidence 规则

交付：

- 本文档定稿
- 前端类型定义
- 后端 result block 映射
- 路由测试用例

退出条件：

- 每类问题都有唯一工具链路
- 每个工具输出都有唯一 block

### Phase 1：稳定检索到回答的后端链路

目标：

- Agent 能稳定调用 `search_sources/search_content`
- 内容问题能稳定生成带 citation 的回答

工作：

1. 实现 `knowledge.answer`。
2. 增加 knowledge evidence adapter。
3. 增加确定性路由。
4. citation 白名单校验。
5. 空结果、partial、权限失败统一输出。
6. 增加 planner/tool 命中率测试。

退出条件：

- “张三发过什么”稳定调用 `search_sources`
- “合同写了什么”稳定调用 `search_content -> knowledge.answer`
- 回答中的 citation 全部来自实际检索结果
- 无结果时不会调用模型编造答案

### Phase 2：Agent 页面 Block 渲染

目标：

- 页面能显示 source list、content results、answer 和 citations

工作：

1. Agent 页面增加 `blocks` 渲染器。
2. 新增 `SourceListBlock` 组件。
3. 新增 `ContentResultBlock` 组件。
4. `hydrateTerminal()` 读取 observations 并转换为 blocks。
5. 来源支持预览和打开原会话。
6. 兼容旧 answer/citations。

退出条件：

- 元数据问题显示结构化列表。
- 内容问题显示回答和来源。
- 移动端可完整查看。

### Phase 3：实时摘要和展示时序

目标：

- 工具完成后立即显示摘要
- 页面按问题类型选择主结果

工作：

1. `step.succeeded` 增加脱敏 `result_preview`。
2. Planner/Task result 增加 `presentation_mode`：
   - `source_list`
   - `answer_with_sources`
   - `action_result`
3. 前端先渲染摘要，终态补拉完整结果。
4. 实现“无证据不展示来源”规则。

退出条件：

- 多步任务执行中能看到已完成检索摘要。
- 元数据问题不生成散文回答。
- 内容问题不会只显示来源列表。

### Phase 4：合并 QA 页面并退役旧入口

前提：

- 旧 QA 历史已清空
- 不需要迁移问答历史

工作：

1. `/chat` 作为唯一入口。
2. `/rag-chat` 重定向到 `/chat?mode=qa`。
3. Agent 页面增加“快速问答 / Agent 任务”模式。
4. 历史列表只保留 Agent Task。
5. Phase 1 稳定后，快速问答也通过 Agent 完成。
6. 删除前端 `askQaStream` 主路径。
7. 保留 RAG `/ai/documents/stream` 作为内部兼容接口或后续下线。

退出条件：

- 页面只存在一个对话入口。
- 两类历史统一到 Agent Task。
- 旧 `/rag-chat` 链接可重定向。

## 9. 页面效果

内容问答：

```text
用户：采购合同的违约责任是什么？

Agent · 已完成 2 个步骤                [查看执行过程]

合同第八条约定，违约方需要支付合同总额的 10%…… [1][2]

来源
[1] 采购合同V2.pdf · 第8页
[2] 法务群消息 · 张三 · 7月15日
```

元数据查询：

```text
用户：张三上个月发过哪些文件？

Agent：找到 3 个文件

预算表.xlsx · 财务群 · 8月20日        [预览]
采购合同.pdf · 法务群 · 8月15日       [预览]
项目排期.xlsx · 财务群 · 8月12日      [预览]
```

操作任务：

```text
用户：明天下午三点提醒我开会

Agent
计划：todo.create
等待确认
[确认] [修改] [拒绝]

确认后：待办已创建
```

## 10. 测试和验收

### 工具稳定性

- 20-50 条典型问题路由准确率 > 95%
- 同一问题重复执行选择同一工具链路
- 不发生工具循环
- 工具失败能重试或明确失败

### 回答正确性

- citation 只来自实际检索结果
- 无结果不编造
- partial coverage 明确提示
- protected 权限不足失败关闭

### 页面

- source list 渲染
- content results 渲染
- answer citations 渲染
- 桌面和移动端布局
- 执行轨迹可折叠
- 历史 Task 可恢复为 blocks

## 11. 明确不做

- 不迁移旧 QA 历史，因为已经清空
- 不保留两个独立问答入口
- 不把完整 chunks 放进 SSE
- 不让模型决定用户和组织身份
- 不在第一版实现复杂来源对比视图
- 不在无检索结果时让模型用常识补答案

## 12. 最终固定顺序

```text
Phase 0: 契约、工具映射、确定性路由冻结
Phase 1: 稳定检索和 knowledge.answer
Phase 2: Agent 页面 result blocks
Phase 3: 实时摘要、证据时机和 presentation_mode
Phase 4: /rag-chat 合并到 /chat，统一 Agent Task 历史
```

完成 Phase 1 前，不开放知识工具灰度。

完成 Phase 2 前，不合并页面入口。

完成 Phase 3 前，不把实时检索摘要作为正式交互承诺。
