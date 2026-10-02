# Agent 历史问答功能方案

## 文档状态

- 版本：v0.4
- 状态：Phase 1、Phase 2 已实施
- 目标：为 Agent 页面建立统一的会话、消息、Task 历史模型，并作为后续记忆机制的基础
- 范围：Agent 服务、Agent 数据库 schema、Agent 页面、旧 AI 问答页面退役
- 数据库归属：同一 PostgreSQL 集群下的 `agent` schema
- 历史兼容策略：不兼容旧 AI 问答历史表
- 修订记录：
  - v0.4（2026-10-02）：Phase 2 落地 —— Agent 对话携带并恢复 `conversation_id`，
    侧边栏按页面区分 Agent 会话与旧 QA 历史，支持历史加载、刷新恢复、重命名、删除和
    非终态 Task 的审批/补充信息恢复。
  - v0.3（2026-10-02）：Phase 1 落地 —— Conversation/Message 数据模型、Conversation API、
    Task 与会话关联、终态 Task 到 assistant message 的投影、以及
    `20261002_agent_conversation_history` 迁移。
  - v0.2（2026-10-02）：按合并后的代码校正 —— 对齐 `agent_tasks` 的 ID 类型与 Core JWT 鉴权链路；
    修正 `AgentTurnView` 的现状描述；补充 Task↔Message 状态映射、会话创建与幂等规则；
    明确本方案与知识问答（knowledge schema）的边界。
  - v0.1：初稿。

## 1. 背景

当前系统有两套问答入口：

```text
/chat       -> Agent 页面
/rag-chat   -> 旧 AI 问答页面
```

两套数据的模型不同：

```text
旧 AI 问答：
  qa_conversations + qa messages
  一个会话可以包含多轮问答

Agent：
  agent_tasks
  每次用户输入创建一个 Task
  当前页面只在内存中维护 transcript
```

这导致：

- Agent 页面没有真实历史对话
- 离开页面后，当前会话无法恢复
- 旧问答历史和 Agent Task 无法统一展示
- 后续记忆机制没有稳定的会话载体

## 2. 最终目标

建立以下模型：

```text
Conversation  = 用户看到的会话窗口
Message       = 用户或助手可见消息
Agent Task    = 一次用户消息触发的 Agent 执行
Memory        = 从 Conversation / Message 中提炼的上下文
```

核心原则：

```text
一个 Conversation 可以包含多个 Agent Task。
Task 是执行单位，Conversation 是历史与记忆单位。
```

最终入口：

```text
/chat 作为唯一对话页面
```

旧 AI 问答页面保留代码作为参考，但不再作为主入口。

### 2.1 与知识问答的边界

```text
本方案的 Conversation / Message：用户与 Agent 自己的聊天历史。
知识问答（群聊、文档、公司资料）：仍在 knowledge / rag schema，经由知识检索工具回答。
第一阶段不把 Agent 自己的会话历史写入 RAG，也不把 RAG 的检索结果当作对话历史。
```

## 3. 明确不做

- 不迁移旧 AI 问答历史表
- 不继续写旧 `qa_conversations`
- 不把 Conversation / Message 放到 RAG 服务
- 不让 RAG 拥有 Agent 会话、Task、审批或记忆
- 不在第一阶段实现复杂长期记忆
- 不删除旧 AI 问答页面代码
- 不在第一阶段实现会话分享、导出、置顶

## 4. 数据库归属决策

推荐方案：

```text
同一个 PostgreSQL 集群
独立 agent schema
```

原因：

```text
1. Conversation / Message / Task 的写入事务紧密关联。
2. Agent 服务应该拥有自己的历史与记忆。
3. RAG 只负责解析、索引、检索和返回授权结果。
4. schema 隔离已经满足服务边界，暂时不需要独立数据库实例。
```

目标结构：

```text
info-agent database
├─ agent schema
│  ├─ conversations
│  ├─ messages
│  ├─ agent_tasks
│  ├─ agent_approvals
│  ├─ agent_observations
│  └─ memory_records
├─ knowledge schema
├─ rag schema
└─ rag_mvp schema
```

## 5. 目标数据模型

### 5.1 conversations

```sql
CREATE TABLE agent.conversations (
  id uuid PRIMARY KEY,
  owner_user_id text NOT NULL,      -- 与 agent.agent_tasks.owner_user_id 保持一致（TEXT）
  organization_id text NULL,        -- 现有 agent 表以 TEXT 存组织 id
  title varchar(200) NOT NULL DEFAULT '新的对话',
  status varchar(32) NOT NULL DEFAULT 'active',
  source varchar(32) NOT NULL DEFAULT 'agent',
  summary text NULL,
  summary_cursor integer NOT NULL DEFAULT 0,
  last_message_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

说明：

- `source` 预留给未来的历史来源标识。
- `summary` 和 `summary_cursor` 为后续记忆机制预留。
- `last_message_at` 用于历史列表排序。
- v0.2 校正：新表自身的主键用 `uuid`；但**凡是引用现有 `agent_tasks` 的列**
  （`owner_user_id`、`task_id`）必须沿用现有的 `TEXT` 类型，否则外键与查询会失配。
  把整个 agent schema 迁到 uuid 主键是独立的一次迁移，不在本方案范围。

### 5.2 messages

```sql
CREATE TABLE agent.messages (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES agent.conversations(id) ON DELETE CASCADE,
  role varchar(32) NOT NULL,
  content text NOT NULL DEFAULT '',
  status varchar(32) NOT NULL DEFAULT 'pending',
  task_id text NULL,                -- 指向 agent.agent_tasks(task_id)，该列是 TEXT
  citations jsonb NOT NULL DEFAULT '[]'::jsonb,
  client_message_id varchar(128) NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

字段含义：

```text
role: user | assistant | system
status: pending | streaming | completed | failed | cancelled
```

### 5.3 agent_tasks 扩展

在现有 `agent.agent_tasks` 上增加：

```sql
ALTER TABLE agent.agent_tasks ADD COLUMN conversation_id uuid NULL;
ALTER TABLE agent.agent_tasks ADD COLUMN request_message_id uuid NULL;
ALTER TABLE agent.agent_tasks ADD COLUMN response_message_id uuid NULL;
```

说明（v0.2 补充）：`conversation_id` / `request_message_id` / `response_message_id` 是新引入的
uuid 列；`task_id` 保持现有 `TEXT` 类型不变，`agent.messages.task_id` 以 `TEXT` 引用它。
迁移文件沿用仓库约定，成对提交：`db/migrations/<yyyymmdd>_agent_conversation_history.{up,down}.sql`。

后续稳定后可以加：

```sql
ALTER TABLE agent.agent_tasks ALTER COLUMN conversation_id SET NOT NULL;
```

### 5.4 memory_records

第一阶段只建表或预留，不启用复杂抽取。

```sql
CREATE TABLE agent.memory_records (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  organization_id uuid NULL,
  scope varchar(32) NOT NULL,
  content text NOT NULL,
  source_message_id uuid NULL,
  confidence numeric(4,3) NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

## 6. API 设计

鉴权与隔离（v0.2 补充）：所有新接口都走 Core JWT（`Depends(current_user)` / `current_user_id`），
服务端只按 `owner_user_id` 读写；用户 A 不得读取、修改或删除用户 B 的会话与消息。组织信息沿用
`core_client.current_organization()`，不信任客户端传入的 `organization_id`。

### 6.1 Conversation API

```text
POST   /api/agent/v1/conversations
GET    /api/agent/v1/conversations
GET    /api/agent/v1/conversations/{conversation_id}
PATCH  /api/agent/v1/conversations/{conversation_id}
DELETE /api/agent/v1/conversations/{conversation_id}
```

`GET /conversations` 返回：

```json
{
  "items": [
    {
      "conversation_id": "uuid",
      "title": "青云官网部署",
      "last_message_at": "2026-10-02T10:00:00Z",
      "message_count": 6
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

`GET /conversations/{id}` 返回：

```json
{
  "conversation_id": "uuid",
  "title": "青云官网部署",
  "messages": [
    {
      "message_id": "uuid",
      "role": "user",
      "content": "青云官网当前在哪个阶段了？",
      "status": "completed",
      "task_id": "uuid",
      "created_at": "..."
    },
    {
      "message_id": "uuid",
      "role": "assistant",
      "content": "本地知识里显示...",
      "status": "completed",
      "task_id": "uuid",
      "citations": [],
      "created_at": "..."
    }
  ]
}
```

### 6.2 Task API

保留现有 Task 接口，并增加会话关联：

```text
POST /api/agent/v1/tasks
```

请求体增加：

```json
{
  "conversation_id": "uuid",
  "client_message_id": "uuid",
  "text": "用户问题",
  "source_type": "chat"
}
```

会话创建与幂等（v0.2 补充）：

- 前端首次发送时可以先 `POST /conversations` 拿到 `conversation_id`，再带着它创建 Task；
- 也可以不带 `conversation_id` 直接创建 Task，由服务端在同一事务里创建会话，并在响应中回传
  `conversation_id`（推荐，少一次往返）；
- 两种方式都必须复用现有的幂等键 `source_type:owner_user_id:client_message_id`：同键重放返回
  同一个 Task 和同一个 Conversation，不得产生第二个会话；
- 带 `conversation_id` 时必须校验它属于调用者，否则返回 403。

保留：

```text
GET /tasks/{task_id}
GET /tasks/{task_id}/events
GET /tasks/{task_id}/observations
```

## 7. 执行流程

### 7.1 新会话

```text
用户点击“新对话”
-> 前端清空当前 transcript
-> 不立即创建数据库 conversation
```

### 7.2 首次发送

```text
没有 conversation_id
-> POST /conversations
-> POST /tasks(conversation_id)
```

### 7.3 追问

```text
已有 conversation_id
-> POST /tasks(conversation_id)
```

同一个 Conversation 下会出现多个 Task：

```text
conversation_id: c-123
├─ task_id: t-001
├─ task_id: t-002
└─ task_id: t-003
```

### 7.4 审批 / 补充信息

```text
不新建 conversation
不新建 task
继续同一个 task
更新同一个 assistant message 的状态
```

### 7.5 Task 完成

```text
更新 assistant message
-> 写入 answer / citations / status
-> 更新 conversation.last_message_at
-> 触发 conversation summary 更新任务
```

### 7.6 Task 事件到 Message 的映射（v0.2 补充）

```text
task.accepted   -> 写入 user message(status=completed)
                -> 写入 assistant message(status=pending)
task.step_started / step_succeeded / step_failed
                -> 不改写 message 内容，只作为"执行过程"展示
task.waiting_input / task.waiting_approval
                -> assistant message 保持 pending，前端按 Task 状态渲染输入/审批卡片
task.completed  -> assistant message: content=answer、citations=...、status=completed
                -> 更新 conversation.last_message_at
                -> 触发 conversation summary 更新任务（Phase 4）
task.failed     -> assistant message: status=failed、content=错误摘要
```

即：**Message 只承载用户可见的问答内容，Step / Observation 不进入 messages。**

## 8. Agent 页面改造

### 8.1 路由

```text
/chat
```

保留 query 支持：

```text
/chat?conversation=<conversation_id>
```

后续：

```text
/rag-chat -> redirect /chat
```

### 8.2 侧边栏历史

历史列表数据源改为：

```text
GET /conversations
```

展示：

- 标题
- 最后更新时间
- 可选：最后一条消息摘要

操作：

```text
点击 -> 加载会话
新对话 -> 清空当前会话
```

Phase 2 已接入：

- 读取历史
- 打开历史
- 新建会话
- 重命名
- 删除

仍暂不做：

- 置顶
- 搜索

### 8.3 消息渲染

回答 / 来源 / 执行过程继续由 `InfoAgentChatPage.vue` 内的同一套轮次渲染函数承载；
实时 SSE 与历史 `GET /conversations/{id}` 都恢复为同一份页面状态。后续如果需要独立组件，
可以再抽 `AgentTurnView.vue`，不阻塞 Phase 2 的页面闭环。

组件职责：

```text
回答优先
来源卡片
执行过程折叠
metadata 轻提示
```

历史加载时：

```text
messages -> 页面轮次状态
```

非终态 Task：

```text
拉取 task + observations
恢复状态卡片
```

## 9. 旧 AI 问答页面处理

不删除 `InfoQuickQAPage.vue`。

处理方式：

```text
1. 从导航和主入口隐藏
2. /rag-chat 后续重定向到 /chat
3. 旧 API 不再作为主链路
4. 旧页面保留作为参考
```

可借鉴：

- citation 卡片
- 附件中置大预览
- 查看来源会话
- 复制问题 / 复制回答
- 空状态与错误状态
- 会话消息加载结构

不继承：

- `qa_conversations` 作为主历史表
- `askQaStream` 作为主问答链路
- 独立的旧知识库选择状态

## 10. 记忆机制规划

### 10.1 短期上下文

```text
Conversation 最近 N 条 messages
```

### 10.2 会话摘要

```text
conversation.summary
conversation.summary_cursor
```

更新时机：

```text
每 N 轮
或 Task 完成后
```

### 10.3 长期记忆

```text
memory_records
```

抽取内容：

- 用户偏好
- 稳定项目事实
- 组织流程
- 负责人
- 技术决策
- 重要日期

每条记忆必须带：

```text
source_message_id
scope
confidence
```

### 10.4 规划输入

Planner / Answer 不应读取全部历史。

输入建议：

```text
当前用户问题
+ conversation.summary
+ 最近 N 条 messages
+ 命中的长期记忆
```

## 11. 分阶段实施

### Phase 1：会话骨架

- 一个 migration 对：`db/migrations/<yyyymmdd>_agent_conversation_history.{up,down}.sql`
- 新建 `agent.conversations`
- 新建 `agent.messages`（ID 类型按 5.x 的 v0.2 校正）
- `agent_tasks` 增加 `conversation_id` / `request_message_id` / `response_message_id`
- 新增 Conversation API（Core JWT + owner 隔离）
- `POST /tasks` 接受 `conversation_id`，按 7.6 的映射写入 message

### Phase 2：Agent 页面历史

- 在 `InfoAgentChatPage.vue` 内统一复用现有轮次渲染（回答 / 来源 / 执行过程 / metadata）
- 侧边栏读取 `GET /conversations`
- 点击历史加载消息（`GET /conversations/{id}` → 恢复为页面轮次）
- 当前新 Task 完成后刷新历史列表

### Phase 3：旧入口退役

- 隐藏旧 AI 问答入口
- `/rag-chat` 重定向 `/chat`
- 旧页面和数据表保留但不写入

### Phase 4：记忆

- conversation summary
- 最近上下文裁剪
- memory_records 抽取
- Planner / Answer 接入 memory context

## 12. 测试与验收

### 数据库

- conversation 创建成功
- user message / assistant message 写入正确
- 多个 Task 关联到同一个 conversation
- 删除 conversation 级联删除 messages

### API

- 历史列表按 last_message_at 倒序
- 打开会话返回完整消息顺序
- 分页正常
- 首页并发发送不会串 conversation
- 用户 A 无法读取 / 修改 / 删除用户 B 的会话（返回 403）
- 同一 `client_message_id` 重放返回同一个 conversation 与 task

### 前端

- 新对话可发送第一问
- 同一会话内追问
- 刷新页面后历史能恢复
- 点击历史正确加载消息
- 非终态 Task 能恢复审批/输入卡片
- `/rag-chat` 重定向后不丢历史

### 记忆

- summary 更新不影响原始消息
- summary_cursor 不重复总结
- 长期记忆带来源 message_id
- 删除会话后记忆策略明确

## 13. 待审核决策

> v0.2 只做"与现有代码对齐"的校正，以下决策项仍未拍板。

1. 新表放在 `agent` schema，是否确认？
2. 一个 Conversation 多 Task，是否确认？
3. ~~第一阶段是否只做历史读取，不做重命名/删除？~~ 已决策：后端支持重命名/删除，前端在 Phase 2 接入。
4. 旧 AI 问答页是否先隐藏，第二阶段再重定向？
5. 长期记忆第一阶段是否只建表不启用？

## 14. 推荐结论

```text
不复用旧问答历史表。
在 agent schema 新建 conversations / messages。
agent_tasks 关联 conversation_id。
/chat 成为唯一会话窗口。
旧页面保留代码，不保留旧历史链路。
```

v0.2 补充前提：新表的引用列沿用现有 `agent_tasks` 的 `TEXT` 类型；新接口全部走 Core JWT 并做
owner 隔离。Phase 2 暂不强制抽取独立 `AgentTurnView` 组件。
