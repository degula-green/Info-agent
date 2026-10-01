# Info-Agent V3 服务架构理解

## 概览

Info-Agent V3 由三个核心微服务组成，通过 FastAPI + Redis Streams + PostgreSQL 构建了一个完整的智能代理系统：

1. **Intent Service** - 意图理解服务（Laya多语言分类器）
2. **Agent Service** - 智能代理服务（任务编排与执行）
3. **RAG Service** - 检索增强生成服务（知识检索与答案生成）

---

## 1. Intent Service（意图理解服务）

### 核心职责
- 使用 Laya 多语言模型进行意图分类
- 为 Agent 服务提供意图识别能力
- 作为 sidecar 服务独立部署

### 技术栈
- **框架**: laya-serve (基于 TypeSafe 兼容的 HTTP API)
- **模型**: `convaiinnovations/laya-multilingual`
- **接口**: `POST /v1/systemone`
- **端口**: 8110

### 架构特点
- **轻量级**: 单文件 `app.py`，不包含业务逻辑
- **模型加载**: 支持本地模型路径或 HuggingFace Hub
- **设备支持**: auto/cpu/cuda 可配置
- **预加载**: 启动时可选预加载模型

### 配置要点
```
LAYA_HOST=0.0.0.0
LAYA_PORT=8110
LAYA_DEVICE=auto
LAYA_PRELOAD=true
LAYA_MODEL_PATH=<本地路径>
LAYA_REVISION=<Hub commit pin>
```

### 与 Agent 的集成
- Agent 通过 `AGENT_LAYA_BASE_URL` 调用此服务
- Agent 本身不安装 Torch/Transformers，保持轻量

---

## 2. Agent Service（智能代理服务）

### 核心职责
- **任务编排**: 理解用户输入，生成执行计划
- **能力调度**: 调用各种 Capability（工具）执行任务
- **状态管理**: 跟踪任务生命周期和执行状态
- **审批流程**: 处理需要用户确认的操作

### 架构层次

#### 2.1 入口层（Ingress）
- **HTTP API**: FastAPI 提供 RESTful 接口
- **主要路由**:
  - `/api/agent/v1/tasks` - 任务 CRUD
  - `/api/agent/v1/tasks/{task_id}/events` - SSE 事件流
  - `/api/agent/v1/approvals` - 审批管理
  - `/health` - 健康检查

#### 2.2 应用层（Application）
**TaskService** (`task_service.py`)
- 任务创建、查询、列表
- 观察记录（Observations）管理
- 计划（Plan）管理

**ExecutionService** (`execution_service.py`)
- 运行时调度：`run_task()`
- Worker 唤醒处理：`handle_wakeup()`
- Outbox 事件分发：`dispatch_outbox()`
- 未完成任务恢复：`resume_unfinished_tasks()`

**KnowledgeEventService** (`knowledge_events.py`)
- 处理来自 Knowledge 服务的事件
- 集成 Knowledge 内容到 Agent 工作流

#### 2.3 核心层（Kernel）
**AgentRuntime** (`kernel/runtime.py`)
- 任务执行主循环
- 步骤状态机：pending → running → completed
- 理解层集成（Understanding Provider）
- 计划生成与绑定

**CapabilityExecutor** (`kernel/executor.py`)
- 实际执行 Capability 调用
- 超时控制
- 异常处理

**ApprovalGateway** (`kernel/approval.py`)
- 审批请求创建
- 审批决策处理
- 版本冲突检测

#### 2.4 能力层（Capabilities）
当前实现的 Capabilities：

**answer.compose** (`capabilities/answer.py`)
- 组织检索到的证据生成带来源的回答
- 引用验证（防止模型编造来源）
- 只读能力，不写入任何数据

**todo.create** (`capabilities/todo.py`)
- 创建待办事项
- 支持时区和时间解析

**web.fetch** (`capabilities/web.py`)
- HTTP 网页抓取
- 支持重定向、超时、私有地址控制

**web.extract** (`capabilities/web.py`)
- 从 HTML 提取结构化内容
- 为 answer.compose 准备证据

#### 2.5 规划层（Planning）
**DeterministicPlanner** (`planning/deterministic.py`)
- 基于规则的计划生成
- 默认规划器

**OpenAICompatiblePlanner** (`planning/llm.py`)
- 基于 LLM 的计划生成
- 支持 JSON Schema 约束
- 可配置通过 `AGENT_PLANNER_PROVIDER=llm`

#### 2.6 理解层（Understanding）
多种理解提供者：
- **RuleBasedUnderstandingProvider**: 规则匹配
- **LayaUnderstandingProvider**: 使用 Intent 服务
- **OpenAICompatibleUnderstandingProvider**: LLM 理解
- **HybridUnderstandingProvider**: Laya + LLM 降级

#### 2.7 基础设施层（Infrastructure）
**PostgreSQL Store** (`infrastructure/postgres/store.py`)
- 任务持久化
- 计划、步骤、观察记录存储
- 审批记录
- 待办事项（Todo）存储

**Redis Streams** (`infrastructure/redis/streams.py`)
- 任务唤醒队列
- Outbox 事件发布
- Worker 消费

**LLM Client** (`infrastructure/llm/client.py`)
- OpenAI 兼容接口
- 支持结构化输出（JSON Schema）

**Laya Client** (`infrastructure/laya/client.py`)
- 调用 Intent 服务
- 意图分类

**Knowledge Client** (`infrastructure/knowledge/client.py`)
- 查询 Knowledge 服务
- 获取知识内容

### 数据模型

**Task（任务）**
```python
task_id: str
owner_user_id: str
status: pending/running/waiting_input/waiting_approval/completed/failed/cancelled
source_type: chat/knowledge_event
payload: dict  # 包含 text, steps 等
created_at, updated_at
```

**Plan（计划）**
```python
plan_id: str
task_id: str
steps: list[Step]  # 执行步骤序列
```

**Step（步骤）**
```python
step_id: str
capability: str  # 能力名称，如 "web.fetch"
arguments: dict
status: pending/running/completed/failed
output: dict | None
```

**Approval（审批）**
```python
approval_id: str
task_id: str
capability: str
arguments: dict
status: pending/approved/rejected
version: int  # 乐观锁
```

### Worker 机制
`worker.py` - Redis Streams 消费者
- 监听 `agent.task.wakeup` 事件
- 从 PostgreSQL 重新加载任务状态
- 调用 `ExecutionService.handle_wakeup()`
- 恢复未完成任务（启动时）
- 信号处理（SIGINT/SIGTERM）

### 配置要点
```
AGENT_DATABASE_URL=postgresql://...
AGENT_REDIS_URL=redis://...
AGENT_UNDERSTANDING_PROVIDER=hybrid  # rules/laya/llm/hybrid
AGENT_PLANNER_PROVIDER=deterministic  # deterministic/llm
AGENT_LLM_BASE_URL=...
AGENT_LAYA_BASE_URL=http://localhost:8110
AGENT_KNOWLEDGE_BASE_URL=...
```

---

## 3. RAG Service（检索增强生成服务）

### 核心职责
- **内容处理**: 解析、分块、嵌入知识内容
- **向量检索**: BM25 + kNN 混合检索
- **分支过滤**: 基于用户权限的内容过滤
- **答案生成**: RAG 驱动的问答

### 架构层次

#### 3.1 入口层
**HTTP API** (`app/main.py`)
- `/search` - 搜索接口
- `/api/v1/search/global` - BM25 + kNN + RRF
- `/api/v1/search/knowledge` - 仅 BM25
- `/api/v1/search/tree` - 树模式（shadow/boost）
- `/api/v1/ai/documents` - RAG 答案生成
- `/api/v1/contact_profile` - 联系人档案
- `/api/v1/admin` - 管理接口

#### 3.2 应用层（Application）

**RAGRetrievalService** (`rag_service.py`)
- 搜索请求处理
- 时间窗口解析
- 分支解析（Branch Keys）
- 实体匹配（Entity Matching）
- 多路检索聚合（BM25 + kNN）
- RRF（Reciprocal Rank Fusion）排序
- 权限检查集成

**ParseService** (`parse_service.py`)
- 内容源获取
- 文档解析
- 分块（Chunking）
- 持久化到 `rag_mvp.chunks`

**IndexService** (`index_service.py`)
- 嵌入向量生成
- 实体匹配
- 分支键生成
- Elasticsearch 批量索引

**MemoryService** (`memory_service.py`)
- 候选实体发现
- 内存相关处理

**EntityService** (`entity_service.py`)
- 实体匹配逻辑
- 受控实体关联

**QAService** (`qa_service.py`)
- RAG 答案生成
- 上下文组装
- 引用提取

**CallbackService** (`callback_service.py`)
- 处理完成回调
- Outbox 事件发送

**Runtime** (`runtime.py`)
- 三车道处理架构：
  - **Parse Lane**: 解析 → 分块
  - **Index Lane**: 嵌入 → 索引
  - **Memory Lane**: 实体发现
- 有界内存队列
- PostgreSQL 作业租约恢复

#### 3.3 领域层（Domain）
**SearchRequest/SearchResult** (`domain/rag.py`)
- 搜索请求模型
- 检索结果模型
- 时间桶计算

**Chunk Models**
- 分块元数据
- 投影状态

#### 3.4 基础设施层（Infrastructure）

**PostgreSQL** (`infrastructure/rag_postgres.py`)
- Schema: `rag_mvp`
- 表：
  - `processing_jobs` - 作业队列
  - `chunks` - 分块存储
  - `chunk_projections` - 投影状态
  - `outbox_events` - 事件发件箱
- 视图/别名：
  - `rag_chunks_display_v1` / `_read` / `_write`
  - `rag_chunks_protected_v1` / `_read` / `_write`

**Elasticsearch** (`infrastructure/rag_elasticsearch.py`)
- 索引：
  - `rag_chunks_display_v1`
  - `rag_chunks_protected_v1`
- 查询：
  - BM25（全文搜索）
  - kNN（向量相似度）
- 过滤：
  - 权限键过滤
  - 分支键过滤

**Redis Streams** (`infrastructure/events/redis_streams.py`)
- 监听 `knowledge.ready` 事件
- 分发到处理队列

**Embedding Provider** (`infrastructure/embedding/`)
- 向量嵌入生成
- 批量处理

**Authorization Gateway** (`mvp_ports.py`)
- 调用 Core 服务的 `search-scope` 和 `check-batch`
- 获取授权的保护对象键
- 权限失败时关闭（fail closed）

#### 3.5 处理流程

```
knowledge.ready 事件
  ↓
Dispatcher: 持久化 processing_jobs → ACK Redis
  ↓
Parse Lane: 获取源 → 解析 → 分块 → 持久化 chunks
  ↓
Index Lane: 嵌入 → 实体匹配 → 分支键 → ES 批量索引
  ↓
ready/metadata_only 回调 → outbox_events
  ↓
Memory Lane: 候选实体发现
```

### Worker 机制
`worker.py` - Redis Streams 消费者
- 监听 `knowledge.ready` 事件
- 构建运行时（Runtime）
- 三车道并发处理
- 失败重连机制（指数退避）
- 日志配置（过滤 elastic_transport 噪音）

### 检索模式

**Tree Mode（树模式）**
- `off`: 禁用分支投票
- `shadow`（默认）: 计算分支诊断但不改变结果
- `boost`: 将分支投票加入 RRF，保留全语料通道

**混合检索**
- BM25: 关键词匹配
- kNN: 语义相似度
- RRF: 排名融合

### 存储契约

**PostgreSQL Schema**: `rag_mvp`
- 分块投影：display（公开）/ protected（受保护）
- 读写别名分离，支持未来投影切换
- 旧 `rag.memory_*` 表被保留但不使用

**Elasticsearch**
- 向量仅存储在 ES 中
- PostgreSQL 存储分块文本、元数据、分支信息

### 边界与集成

**不读取的数据库**
- RAG 不读取 Knowledge 或 Core 数据库

**通过 HTTP 获取内容**
- Knowledge 内容通过内部 HTTP API 获取

**权限委托**
- 通过 Core 服务的 `search-scope` 和 `check-batch` 进行授权
- 受保护分支失败关闭（fail closed）

**API 适配**
- Controller/API 可能接受旧前端请求格式
- 存储和领域代码仅使用冻结的 MVP 契约

### 配置要点
```
RAG_DATABASE_URL=postgresql://...
RAG_DATABASE_SCHEMA=rag_mvp
RAG_ELASTICSEARCH_URL=...
RAG_REDIS_URL=redis://...
RAG_REDIS_INBOUND_STREAM=knowledge.ready
RAG_REDIS_CONSUMER_GROUP=rag-workers
RAG_EMBEDDING_PROVIDER=...
RAG_CORE_BASE_URL=...  # 权限检查
RAG_KNOWLEDGE_BASE_URL=...  # 内容获取
```

---

## 4. 服务间集成关系

### 数据流向

```
用户输入
  ↓
Agent Service (Task 创建)
  ↓
Understanding Layer → Intent Service (意图分类)
  ↓
Planner (生成执行计划)
  ↓
Runtime (执行步骤)
  ↓
Capabilities (web.fetch, web.extract)
  ↓
answer.compose → LLM 生成答案
  ↓
Task 完成 → 事件发布
```

### RAG 集成点（待实现）

**当前状态**: RAG 模块尚未封装为 Agent Capability

**未来集成方向**:
1. **新增 Capability**: `knowledge.search`
   - 调用 RAG Service 的 `/api/v1/search/global`
   - 返回检索结果作为 evidence

2. **新增 Capability**: `knowledge.answer`
   - 调用 RAG Service 的 `/api/v1/ai/documents`
   - 直接返回 RAG 生成的答案

3. **集成到现有流程**:
   ```
   Task 创建
     ↓
   Planner 识别需要知识检索
     ↓
   Step 1: knowledge.search (调用 RAG)
     ↓
   Step 2: answer.compose (基于 RAG 结果)
     ↓
   返回带来源的答案
   ```

4. **权限集成**:
   - Agent 传递 `x_user_id` 和 `x_organization_id` 到 RAG
   - RAG 通过 Core 服务验证权限
   - 返回过滤后的结果

### 事件驱动架构

**Redis Streams 作为消息总线**:
- `agent.task.wakeup` - Agent Worker 消费
- `knowledge.ready` - RAG Worker 消费
- `agent.outbox` - Agent 事件发布

**Outbox Pattern**:
- PostgreSQL 事务性写入 outbox 表
- 后台 Dispatcher 轮询并发布到 Redis
- 保证至少一次交付

### 共享基础设施

**PostgreSQL**:
- Agent Schema: `agent` (tasks, plans, steps, approvals, todos)
- RAG Schema: `rag_mvp` (chunks, processing_jobs, outbox_events)

**Redis**:
- 共享 Redis 实例
- 不同 Stream 名称隔离

**Knowledge Service**:
- Agent 调用: 获取知识内容
- RAG 调用: 获取待处理内容

**Core Service**:
- RAG 调用: 权限检查

---

## 5. 关键设计模式

### 5.1 能力注册表模式（Capability Registry）
- 所有能力统一注册
- 描述符驱动（Descriptor-driven）
- 超时、风险等级、审批要求声明式配置

### 5.2 计划-步骤-观察模式
- Plan: 声明式步骤序列
- Step: 单个能力调用单元
- Observation: 执行结果记录

### 5.3 输入绑定模式（Input Binding）
- Planner 使用引用：`evidence_ref="$steps.2.output.evidence"`
- Runtime 解析引用并绑定实际数据
- 避免重复序列化大数据

### 5.4 审批网关模式（Approval Gateway）
- 高风险操作需要用户审批
- 乐观锁版本控制
- 超时自动失效

### 5.5 三车道处理模式（Three-Lane Processing）
- Parse Lane: 快速解析分块
- Index Lane: 慢速嵌入索引
- Memory Lane: 实体发现
- 有界队列防止内存溢出
- PostgreSQL 作业租约保证恢复

### 5.6 投影别名模式（Projection Alias）
- 读写别名分离
- 支持零停机迁移
- 版本化投影（v1, v2...）

### 5.7 混合理解模式（Hybrid Understanding）
- 快速分类器（Laya）优先
- LLM 降级兜底
- 规则匹配最快路径

---

## 6. 已知问题与记忆

根据 MEMORY.md 中的记录：

### 6.1 时间解析陷阱
- 查询中的"年/月"会被作为硬时间窗口过滤所有分支
- 这是最大的检索丢失来源

### 6.2 数据库迁移
- Compose 迁移列表已过时
- 新的 .sql 文件不会被应用
- 修复必须在现有 schema 内完成

### 6.3 RAG Worker 诊断
- 待处理条目没有 processing_jobs 行 = 失败发生在处理器重试会计之外
- Worker 组跨机器运行：一个公网 Redis + 多台笔记本
- 陈旧的远程 Worker 会静默阻塞积压
- Worker 死亡静默：检查进程和 .err 文件修改时间，而非日志

### 6.4 实体树退化
- 根基数 + 常量下层键
- 月份内部结构无法修复实体树

---

## 7. 下一步集成建议

### 7.1 短期：RAG 能力封装
1. 实现 `knowledge.search` Capability
   - 输入：query, top_k, scope
   - 输出：evidence (符合 answer.compose 格式)
   - 调用 RAG Service `/api/v1/search/global`

2. 实现 `knowledge.answer` Capability
   - 输入：question, scope
   - 输出：answer, citations
   - 调用 RAG Service `/api/v1/ai/documents`

3. 更新 Planner
   - 识别需要知识检索的任务
   - 生成包含 knowledge.search 的计划

### 7.2 中期：深度集成
1. 权限透传
   - Agent 传递用户 ID 和组织 ID
   - RAG 验证并过滤结果

2. 分支感知
   - Agent 识别查询中的时间/实体
   - 传递给 RAG 作为分支提示

3. 实体链接
   - Agent 识别的实体传递给 RAG
   - RAG 使用实体进行精确匹配

### 7.3 长期：统一工作流
1. 多模态能力
   - 图像理解
   - 音频处理

2. 长会话记忆
   - 跨任务上下文
   - 用户偏好学习

3. 主动推荐
   - 基于历史任务推荐新任务
   - 知识内容推荐

---

## 8. 技术栈总结

| 层次 | Intent | Agent | RAG |
|------|--------|-------|-----|
| **语言** | Python | Python | Python |
| **框架** | laya-serve | FastAPI | FastAPI |
| **存储** | - | PostgreSQL | PostgreSQL |
| **缓存** | - | Redis Streams | Redis Streams |
| **搜索** | - | - | Elasticsearch |
| **模型** | Laya Multi | OpenAI API | OpenAI API + Embedding |
| **Worker** | - | Redis Consumer | Redis Consumer |
| **端口** | 8110 | (配置) | 8000 |

---

## 9. 总结

**Info-Agent V3** 是一个模块化、事件驱动的智能代理系统：

- **Intent Service** 提供轻量级意图分类
- **Agent Service** 是任务编排和执行引擎，具备能力扩展机制
- **RAG Service** 是知识检索和答案生成引擎，支持混合检索和权限控制

当前 **RAG 尚未作为 Capability 集成到 Agent**，这是下一步的重点工作。集成完成后，Agent 将能够无缝调用知识检索能力，实现真正的 RAG 驱动的智能问答。

关键架构优势：
- ✅ 清晰的关注点分离
- ✅ 可扩展的能力注册机制
- ✅ 事件驱动的异步处理
- ✅ 声明式计划生成
- ✅ 权限安全的知识检索

待改进点：
- ⚠️ RAG 能力封装
- ⚠️ 时间解析优化
- ⚠️ Worker 监控增强
- ⚠️ 数据库迁移流程
