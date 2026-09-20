# RAG 表结构及 ES 索引设计

> 状态：初版讨论稿。本文只设计服务三 RAG 层所需的 PostgreSQL 数据结构和 Elasticsearch 检索投影，具体字段、约束和容量参数需要结合实现与压测继续调整。

## 1. 设计边界

- PostgreSQL 是 RAG 记忆结构、Fact、Chunk 关系、版本和处理状态的权威来源。
- Elasticsearch 是在线检索投影，保存可用于 BM25、向量和层级过滤的数据。
- 原始消息、原始附件和业务知识对象继续由 `knowledge` schema 管理。
- OpenFGA 仍由服务一负责最终授权；ES 中的授权对象字段只用于检索前过滤，不能代替实时授权。
- Fact 只保存一份，通过关系挂载到多个 Session、Entity、Scene Tree。
- 敏感附件仍然抽取、Chunk、Fact 和向量化，但未授权内容不能参与受保护内容召回。

## 2. 现有表的复用和调整

### 2.1 直接复用

| 现有表 | 在新 RAG 流程中的职责 |
|---|---|
| `knowledge.knowledge_bases` | 知识库、组织/私人范围和生命周期来源 |
| `knowledge.conversation_ingestions` | 会话采集范围、群聊/私聊和组织归属 |
| `knowledge.messages` | 原始消息规范化后的业务来源、发送人、发送时间和内容版本 |
| `knowledge.message_sources` | 消息与采集来源、采集时间和原始载荷追踪 |
| `knowledge.attachments` | 附件元数据、对象地址、解析版本、敏感级别和附件 ACL 版本 |
| `knowledge.knowledge_items` | 统一知识对象、内容变体、可见性、组织/所有者和权限版本 |
| `knowledge.outbox_events` | PG 提交后的可靠事件通知，触发服务三处理或索引更新 |
| `rag.search_history` | 查询审计和检索耗时记录 |
| `rag.qa_conversations` / `rag.qa_messages` | AI 对话及引用结果持久化 |

### 2.2 建议小幅扩展

以下表不改变原有业务职责，只增加 RAG 所需的关联或处理信息：

- `knowledge.messages`：可增加采集时间/业务发生时间的明确语义，或在 RAG 来源表中统一保存；不建议把 Chunk、Fact 直接塞入消息表。
- `knowledge.attachments`：继续使用现有 `extracted_original_ref`、`extracted_display_ref`、`sensitivity`、`content_access_required`、`acl_version` 和 `processing_status`。
- `knowledge.knowledge_items`：继续使用 `content_visibility`、`original_access_required`、`acl_version`、`security_status` 和 `processing_status`，作为 Chunk/Fact 的权限和版本来源。
- `rag.processing_jobs`：增加或扩展 job 类型以覆盖 `chunk`、`fact`、`tree_update`、`summary`、`embedding`、`index`；保留现有重试、阶段和版本字段。
- `rag.index_records`：继续记录知识对象的展示/受保护索引状态；如果树投影独立维护，应增加对象类型或另建树投影状态表，不把所有树文档强行压入 Chunk 记录。

## 3. 需要新增的 PG 表

以下是 RAG 记忆层的核心实体。表名为建议名，重点是职责和关系，不代表最终 migration 必须完全照搬。

### 3.1 `rag.memory_trees`

表示一棵具体的记忆树。

核心信息：

- Tree 身份、知识库、组织/租户范围
- `tree_type`：`session`、`entity`、`scene`
- 对应的会话、实体或场景主体
- Root 节点、生命周期和当前版本
- 树的构建策略版本

同一个知识库可以拥有多棵 Tree；一个 Fact 可以通过关系被多棵 Tree 引用。

### 3.2 `rag.memory_nodes`

表示 Tree 中的 Root、Internal Node 和 Leaf Node。

核心信息：

- Node 身份、所属 Tree、`parent_id`
- 层级、节点类型和排序信息
- 时间范围、主题/实体范围
- 当前摘要、摘要版本和摘要生成状态
- dirty 状态、内容版本和最后更新时间

节点类型含义：

- `root`：整棵 Tree 的入口和总摘要
- `internal`：仍有子节点的中间分组
- `leaf`：最末级事实容器

Leaf 不是一条 Fact，也不是一个文件。Leaf 是同一主题、相近时间且权限边界一致的一组 Fact。

### 3.3 `rag.memory_facts`

表示可检索、可引用的原子事实。

核心信息：

- Fact 文本、Fact 类型和规范化版本
- 发生时间/有效时间范围
- 关联实体和场景
- 当前状态：有效、被覆盖、撤销、历史
- 来源知识对象、消息、附件和版本指针
- 内容可见性、敏感级别和授权对象标识

Fact 正文只保存一份。事实演化通过版本关系或前后继关系表达，便于回答“当前状态”和“变化过程”。

### 3.4 `rag.memory_node_facts`

表示 Leaf 与 Fact 的多对多关系。

核心信息：

- `node_id`、`fact_id`
- 在 Leaf 内的顺序、时间位置或相关性位置
- 挂载策略版本和当前状态

同一个 Fact 可在 Session Tree、Entity Tree 和 Scene Tree 中各有一条关系，但不复制 Fact 内容。

### 3.5 `rag.memory_chunks`

表示从消息或附件解析文本得到的 Chunk。Chunk 是原始上下文片段，Fact 是从 Chunk 中抽取的原子事实，两者不能混为一层。

核心信息：

- Chunk 文本、序号、字符/页码/段落定位
- 来源 `knowledge_item`、message、attachment 和内容版本
- 展示文本或原始文本的变体
- 内容可见性、敏感级别、授权对象和 ACL 版本
- 分块版本、解析版本、向量模型版本和处理状态

Chunk 需要保留回溯原始消息或附件的位置，最终上下文优先通过 Chunk 回源，而不是只使用 Fact 摘要。

### 3.6 `rag.memory_fact_chunks`

表示 Fact 与来源 Chunk 的关系。

一个 Fact 可以来自一个或多个 Chunk；一个 Chunk 也可以抽取多个 Fact。该关系支持引用定位、原文回溯和事实解释。

### 3.7 `rag.memory_entities`

表示 Entity Tree 的主体和实体标准化结果。

核心信息：

- 实体类型、标准名称、别名
- 组织/知识库范围
- 外部实体标识和合并状态
- 识别来源、置信度和规则/模型版本

Entity 识别由服务三负责最终确认；服务二提供的实体信息可以作为候选，不直接决定 Tree 挂载。

### 3.8 `rag.memory_scenes`

表示 Scene Tree 的语义场景或主题主体。

核心信息：

- 场景类型、标准名称和规则版本
- 组织/知识库范围
- 场景生命周期和合并状态
- 相关关键词/分类标签

初期可以使用固定场景字典和规则；复杂场景分类可由模型生成候选，再经过枚举和置信度校验。

### 3.9 `rag.memory_tree_memberships`

用于将 Tree 与其主体建立明确关系，避免把 Session、Entity、Scene 的主体字段全部做成可空外键。

核心信息：

- `tree_id`
- 主体类型和主体 ID：session、entity 或 scene
- 生效状态和版本

也可以在 `memory_trees` 中使用互斥的主体列，二者选一，不能同时采用两套语义。

### 3.10 `rag.memory_index_outbox`

建议单独记录树、Fact、Chunk 的 ES 投影更新任务；也可以扩展现有 `knowledge.outbox_events`，但需要保证树摘要更新和 ES 更新的幂等性。

核心信息：

- 对象类型和对象 ID
- upsert/delete 操作
- 对象版本、mapping 版本和目标索引
- 待处理、成功、失败、重试信息

## 4. 关系示意

```text
knowledge.message / attachment / knowledge_item
                │
                ▼
        rag.memory_chunk
                │ 1..N
                ▼
        rag.memory_fact
                │ N..N
                ▼
        rag.memory_node_fact
                │
                ▼
        rag.memory_node ── parent_id ──> rag.memory_node
                │
                ▼
        rag.memory_tree
```

三类 Tree 共享 Fact：

```text
Fact F1
├── Session Tree / Leaf S1
├── Entity Tree / Leaf E4
└── Scene Tree / Leaf C2
```

## 5. ES 索引总体方案

现有项目已经采用展示索引和受保护索引的双索引模式。建议保留这个权限边界，并为层级记忆增加独立的 Root、Node、Fact、Chunk 检索投影。

### 5.1 推荐的物理索引

| 索引 | 作用 |
|---|---|
| `knowledge_display_chunks_v1` | 公开/脱敏文本、公开附件内容和可展示附件元数据的 Chunk；继续沿用现有索引 |
| `knowledge_protected_chunks_v1` | 未脱敏敏感附件或其他受保护原文 Chunk；查询前必须带服务一返回的授权对象过滤 |
| `memory_display_facts_v1` | 公开/脱敏 Fact 的检索投影 |
| `memory_protected_facts_v1` | 受保护 Fact 的检索投影；只有授权资源参与召回 |
| `memory_display_nodes_v1` | 公开安全摘要对应的 Root/Node/Leaf 投影 |
| `memory_protected_nodes_v1` | 受限子树的 Node/Root/Leaf 摘要和向量 |

Root、Node、Leaf 可以先共用 Node 索引，通过 `node_type` 区分；不建议一开始为三种节点分别建物理索引。三种 Tree 也共用同一索引，通过 `tree_type` 和 `tree_id` 区分。

如果后续确认所有父级摘要都经过严格脱敏且不包含受限语义，也可以把安全 Node 合并进展示 Node 索引；初期分开更容易验证权限边界。

### 5.2 为什么不只在 ES 存向量

每个 ES 文档还需要保存最少的检索结构：

- 对象 ID、`tree_id`、`parent_id`、层级和节点类型
- 组织/知识库范围
- 时间范围和实体/场景过滤信息
- 摘要或规范化文本
- 来源指针、可见性和授权对象键
- embedding、模型版本、内容版本和生命周期状态

否则 ES 无法在 Root → Node → Leaf → Fact 过程中限制候选范围，只能退化成全局向量检索。

## 6. ES 文档逻辑

### 6.1 Chunk 投影

继续沿用现有 `knowledge_display_chunks` 和 `knowledge_protected_chunks` 设计。Chunk 是原始上下文检索和最终回源的主要入口，包含文本、向量、来源定位、内容版本和授权对象键。

敏感附件的原始解析 Chunk 正常向量化进入受保护索引；无权限用户只能命中允许公开的元数据或脱敏投影。

### 6.2 Fact 投影

Fact 文档至少需要表达：

- Fact 文本和 embedding
- Fact 状态、事实版本和有效时间
- 所属 Tree/Leaf 的引用
- 来源 Chunk、知识对象和附件标识
- `tree_type`、实体/场景标识
- `visibility`、`auth_object_key` 和 ACL 版本

Fact 是具体答案的主要语义召回层，但必须在已经选定的 Tree/Leaf 范围内查询。

### 6.3 Node 投影

Node 文档至少需要表达：

- Node 摘要和 embedding
- `node_id`、`tree_id`、`parent_id`、level、`node_type`
- `tree_type`、主体 ID、实体/场景标识
- 时间范围和子树版本
- 可见性、授权对象键和生命周期状态

Root 与 Node 使用相同的层级关系字段；Root 的 `parent_id` 为空，Leaf 的 `node_type` 为 `leaf`。

Node/Root 摘要必须遵守权限边界。公开父级摘要只能描述安全信息，不能把敏感附件正文、金额、条款等内容压缩进可公开向量。

## 7. 写入和索引同步

```text
服务二规范化信息写入 knowledge 表
→ outbox/Redis 事件
→ 服务三回 PG 读取权威版本
→ 生成 Chunk
→ 抽取 Fact 并建立 Fact-Chunk 关系
→ 路由并挂载到三类 Tree
→ 更新 Leaf 和祖先摘要
→ 生成 Chunk/Fact/Node 向量
→ 写入对应 display/protected ES 索引
```

事务边界建议：PG 内先提交 Fact、关系、节点摘要版本和待索引事件；ES 更新异步执行。ES 失败时重试或从 PG 重建，不反向修改 PG 权威关系。

更新 Fact 时只更新它所在 Leaf 及其祖先路径。若事实被撤销或被新版本覆盖，需要同步更新事实状态、受影响摘要和 ES 文档状态。

## 8. 查询时的索引使用

```text
Query Understanding
→ 组织/知识库/时间/实体等结构化过滤
→ 服务一返回当前范围内可访问的敏感 resource_id
→ Root/Node 在 display 与已授权 protected 范围内召回
→ 使用 tree_id + parent_id 逐层下钻
→ 在选定 Leaf 范围内检索 Fact
→ 必要时检索关联 Chunk
→ 批量回 PG 获取权威 Fact 和原始上下文
→ 最终权限复核后交给 LLM
```

不能每次固定地把 Session、Entity、Scene 三套 Tree 全量搜索一遍。三类 Tree 共用统一 Root 检索入口，先由结构化条件和 Root 语义匹配选出少量相关 Tree，再继续下钻。

## 9. 权限索引策略

- 公开/脱敏 Chunk、Fact、Node：直接参与展示和语义检索。
- 敏感附件 Chunk、Fact、Node：正常保存向量，但必须用服务一返回的授权 `resource_id/auth_object_key` 做受保护索引过滤。
- OpenFGA 只管理真正受限的附件/原始资源，不要求公开内容逐条进入 OpenFGA。
- 查询前先限定用户所在组织、租户和知识库，再向服务一请求该范围内用户有权限查看的敏感资源集合。
- 权限关系变化只改变可检索集合，不需要重新生成向量；授权集合、检索结果和上下文缓存必须按用户及权限版本失效或短时过期。
- 公开父级 Node/Root 不混入受限正文细节。必要时同一逻辑树保留公开安全摘要和受限完整摘要两种投影。

## 10. 版本和一致性

需要同时追踪以下版本：

- 来源内容版本
- Chunking/解析版本
- Fact 提取版本
- Tree 构建策略版本
- 摘要版本
- Embedding 模型版本
- OpenFGA/ACL 版本
- ES mapping 版本

ES 文档必须携带足够的版本信息，用于判断旧事件是否覆盖新版本。ACL 版本用于缓存和同步校验，但不能替代实时 OpenFGA 判断。

## 11. 待确认问题

1. `rag.memory_chunks` 是否作为 PG 中的完整 Chunk 正文存储，还是只存 Chunk 元数据和对象引用，正文继续由现有解析产物存储？初步建议：PG 保存可重建所需的文本/哈希/定位信息，超大原文仍放对象存储。
2. 展示和受保护的 Fact/Node 是否采用两套物理 ES 索引，还是统一索引加权限过滤？初步建议保留双物理索引，减少受保护内容被错误查询的风险。
3. 公开安全摘要是否与受限完整摘要各自维护，还是统一生成不包含敏感细节的父级摘要？初步建议父级公开摘要统一安全化，受限子树保留完整摘要。
4. `memory_nodes` 的父子关系是否使用单一 `parent_id`，还是需要额外的路径字段/闭包表以支持祖先更新和批量查询？初步建议先使用 `parent_id + level`，祖先更新在 PG 事务中按路径处理。
5. Session Tree 的时间桶采用固定小时/天，还是按消息数量和主题变化动态切分？
6. Entity 和 Scene 的主体是否允许跨知识库复用，还是严格限定在组织/知识库范围内？初步建议默认限定组织和知识库，避免跨租户实体串联。
7. 服务三接管 Entity 识别后，实体候选是否允许调用 few-shot LLM，低置信度结果如何进入人工确认或延迟挂载？
8. Fact 版本链需要保留多久，历史事实是否参与普通检索，还是只在明确询问演化过程时召回？
9. 是否需要为 `memory_fact` 和 `memory_node` 独立配置读写别名、重建策略和安全审计事件？
10. 当前已有 `rag.index_records` 是否扩展为统一的 Chunk/Memory 投影状态，还是保持 Chunk 专用并新增独立的 memory index 状态表？

