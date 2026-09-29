# RAG 消费者链路优化设计

> 状态：MVP 设计基线  
> 范围：`knowledge.ready` 进入 RAG 后，到内容可检索并回写状态为止  
> 原则：单进程、逻辑分 Lane、任务状态持久化、先可搜索后做 Memory

## 1. 优化目标

当前 RAG Worker 同时在一条阻塞链路中完成回源、解析、切块、Embedding、写索引、事实抽取、建树和回调。文字消息会被大型附件解析拖慢，多附件只能顺序处理，单阶段失败也容易导致整条任务重做。

本轮优化要解决以下问题：

1. 文字消息和大型文件分开处理，避免互相阻塞。
2. 一个消息中的正文和附件分别对应独立 KnowledgeItem 和任务，独立成功、失败和重试。
3. 内容写检索索引后立即可搜索，不被 Fact、Tree 和摘要阻塞。
4. A 任务失败不影响同一消息中的 B、C 任务。
5. 前端只需要读取一个任务状态字段。
6. MVP 不拆分多个部署进程，但任务模型必须支持未来拆进程。

本轮不处理：

- 候选 Entity、Fact、摘要和语义树节点的具体生成策略。
- 新的检索排序算法。
- 多 RAG Worker 进程部署。
- 跨服务重新划分原始内容和权限的数据所有权。

## 2. 核心决策

### 2.1 单进程，多逻辑 Lane

MVP 只运行一个 RAG Worker 进程，但进程内部拆成相互独立的逻辑处理通道：

```text
RAG Worker 进程
├── Dispatcher
├── Parse Lane
├── Index Lane
├── Memory Lane
└── Callback Lane
```

Lane 只表示职责边界，不要求 MVP 立即拆成多个服务或进程。每个 Lane 使用独立的有界内存队列和线程池，任务状态保存在 PostgreSQL。

### 2.2 一个 KnowledgeItem 对应一个可处理资源和任务

MVP 明确以下数据契约：

> 一个 KnowledgeItem 对应一个可处理资源，并对应一个 RAG 任务和一个对外状态。

可处理资源包括：

- `message`
- `attachment`

一次采集只产生一个可处理资源。消息和附件分别拥有独立 KnowledgeItem 和任务，任务之间没有执行顺序依赖，失败和重试互不影响。

不额外创建持久化的父任务，也不在 RAG 中设计多个附件共享一个 KnowledgeItem 的状态聚合。如果上游出现一个 KnowledgeItem 携带多个附件，应先在上游拆分，再发布 `knowledge.ready`。

### 2.3 前端只依赖一个状态字段

RAG 的 `processing_jobs.status` 是内部权威状态。

Knowledge 的 `rag_status` 是前端唯一依赖的状态字段，由 RAG 回调更新。Outbox 只负责可靠投递，不作为前端状态来源。

前端不得使用 `processing_jobs.status`、`current_stage`、`rag_job_id`、`rag_finished_at` 或 `rag_result` 拼接列表状态。这些字段只用于内部调度、详情展示和排障。

所有任务统一使用以下状态：

```text
pending
processing
ready
metadata_only
failed
cancelled
```

语义：

| 状态 | 含义 |
|---|---|
| `pending` | 已进入 RAG，等待执行 |
| `processing` | 正在解析、切块或建立索引 |
| `ready` | 已写入检索索引，可以搜索和问答 |
| `metadata_only` | 文件已保存，但只保留元数据，正文不可解析 |
| `failed` | 最终处理失败 |
| `cancelled` | 用户取消、资源删除或版本失效 |

`current_stage` 用于内部诊断，例如 `fetch`、`parse`、`chunk`、`embed`、`index`、`memory`，前端不直接依赖。

当前数据库若暂时仍使用 `succeeded`，它只能作为 `ready` 的旧存储别名，不能同时形成两套业务语义。

### 2.4 Service2 消息队列投递修改

Service2/Knowledge 的 Outbox 和 Redis Stream `knowledge.ready` payload 必须同步增加以下字段：

```text
source_conversation_id
source_conversation_type
source_audience_policy
```

字段规则：

| 字段 | 规则 |
|---|---|
| `source_conversation_id` | Knowledge 内部 `conversation_ingestions.id` UUID，不使用平台外部会话 ID |
| `source_conversation_type` | `group`、`private` 或空 |
| `source_audience_policy` | `source_conversation_members`、`organization_members`、`owner_only` |

投递规则如下：

- 平台会话采集的消息和附件必须携带内部 `source_conversation_id`。
- 附件事件从所属消息或会话继承内部会话 ID 和受众策略。
- 从群聊采集的内容使用 `source_audience_policy=source_conversation_members`。
- 组织文件库内容使用 `source_audience_policy=organization_members`。
- 私人知识库内容使用 `source_audience_policy=owner_only`。
- 私人共享到组织时创建组织副本并使用 `organization_members`。
- 外部飞书、微信或企业微信会话 ID 可以作为来源元数据，但不能作为权限键。
- Redis Stream 中的 envelope 和 `knowledge.outbox_events.payload` 必须保持一致。
- RAG 回源后校验 `source_conversation_id` 与 Knowledge 权威记录一致。

## 3. 目标链路

```text
Knowledge 服务
  └── 保存消息、附件、权限和版本
      └── 发布 knowledge.ready

RAG Dispatcher
  └── 消费 knowledge.ready
      └── 为一个 KnowledgeItem/可处理资源创建一个任务
          └── 状态写入 pending

Parse Lane
  └── 文字：读取正文
  └── 附件：下载、解析、规范化、切块
      └── 写入 Chunk
          └── 投递 Index Lane

Index Lane
  └── Embedding
  └── 写入 Elasticsearch
  └── 写入 conversation_id、document_id、sent_at 等元数据
  └── 对受控 Entity 做确定性精确匹配
  └── 写入确定性 branch_keys
      └── 状态更新为 ready
      └── 回调 Knowledge
      └── 投递 Memory Lane

Memory Lane
  └── 发现和归集候选 Entity
  └── 生成实体合并建议
  └── 后续再确定 Fact、摘要和语义树节点逻辑
      └── 不阻塞 ready 状态

Callback Lane
  └── 消费 RAG Outbox
  └── 更新 Knowledge 的任务状态
```

## 4. Dispatcher 职责

Dispatcher 只负责消费和拆任务，不执行解析、Embedding 或模型调用。

处理顺序：

1. 校验 `knowledge.ready` 的必填字段和版本。
2. 按 `source_event_id` 保证消费幂等。
3. 为一个 KnowledgeItem/可处理资源创建一个 RAG 任务。
4. 任务和对应 Outbox 记录写入 PostgreSQL。
5. 任务成功持久化后，才 ACK Redis Stream 消息。
6. 将任务写入内存 Lane 队列，触发处理。

如果进程在处理前崩溃，任务仍在 PostgreSQL 中，重启后可以重新装载。Redis 消息可以重复投递，但不能造成重复任务。

## 5. 任务模型

MVP 复用 `rag.processing_jobs`，每条记录至少需要：

```text
id
source_event_id
knowledge_item_id
resource_type
resource_id
content_version
acl_version
status
current_stage
retry_count
last_error
created_at
started_at
finished_at
```

其中：

- `resource_type`：`message` 或 `attachment`
- `resource_id`：消息 ID 或附件 ID
- `status`：RAG 内部任务状态
- `current_stage`：RAG 内部诊断字段

一个任务只对应一个 KnowledgeItem，不允许一个任务聚合多个附件的最终状态。

幂等键至少包含：

```text
knowledge_item_id
+ resource_type
+ resource_id
+ content_version
+ processing_version
```

## 6. Lane 与并发

### 6.1 Parse Lane

负责：

- 文字内容读取。
- 附件下载。
- MinerU 或本地解析。
- 文本清理和图片解析。
- Chunk 创建。

解析任务之间并发，单文件内部解析按解析器能力执行。MinerU 等外部解析服务需要独立的并发上限，避免大量文件同时提交。

### 6.2 Index Lane

负责：

- Embedding。
- 批量写 Elasticsearch。
- 写入内部 `source_conversation_id`、外部会话 ID、`document_id`、`sent_at` 等 Chunk 元数据；这些字段用于过滤，不拆成 Session/Document/Time 三棵树。
- 根据 `source_audience_policy` 写入 `auth_partition_key`。
- 对受控实体注册表进行确定性精确匹配和别名匹配。
- 写入 `entity:{entity_id}:{YYYY-MM}` 等确定性 `branch_keys`。
- 无法确认正式 Entity 时保持 Chunk 可检索，不写正式树分支。
- 更新任务为 `ready`。

Embedding 至少有两个并发层次：

```text
多个任务并发
  └── 单任务内多个 Embedding 批次并发
      └── 全局模型调用上限
```

具体批次大小和并发数在实现阶段配置，不在本文固定。

### 6.3 Memory Lane

MVP 只定义边界：

- 只接收已经 `ready` 的任务。
- 不阻塞搜索、问答和前端 `ready` 展示。
- 独立重试，不修改已经生效的 Chunk/Embedding。
- 负责候选 Entity、候选别名、实体合并建议，以及后续 Fact、摘要和语义树节点逻辑。
- 候选 Entity 不进入正式树，也不参与排他过滤。

### 6.4 Callback Lane

负责：

- 消费 RAG Outbox。
- 将任务状态回写 Knowledge。
- 回调失败时重试，不影响内容是否已经可搜索。

## 7. 状态流转

```text
pending
  -> processing
      -> ready
          -> Memory Lane（状态不改变前端 task status）
      -> metadata_only
      -> failed
  -> cancelled
```

状态更新规则：

1. `knowledge.ready` 已发布但 RAG 尚未创建任务时，Knowledge 显示 `pending`。
2. RAG 创建任务后仍为 `pending`。
3. 开始解析时更新为 `processing`。
4. Chunk、Embedding 和基础 ES 索引完成后立即更新为 `ready`。
5. Memory 完成不改变前端状态；失败也不把 `ready` 回退为 `failed`。
6. 文件无法解析正文但元数据已经可靠保存时更新为 `metadata_only`。
7. 只有无法完成基础索引时才更新为 `failed`。

`ready` 的唯一含义是：

> 内容已经可以被搜索和问答使用。

## 8. 单字段状态归属

前端列表只读取 `knowledge_items.rag_status`。

正文和附件分别拥有独立 KnowledgeItem，因此分别拥有独立 `rag_status`，不存在一个 KnowledgeItem 需要展示多个附件状态的问题。

字段职责固定为：

| 字段 | 用途 |
|---|---|
| `knowledge_items.rag_status` | 前端唯一状态依据 |
| `processing_jobs.status` | RAG 内部任务调度 |
| `processing_jobs.current_stage` | RAG 内部阶段诊断 |
| `rag_job_id` | 链路追踪 |
| `rag_last_error` | 失败详情 |
| `rag_finished_at` | 完成时间 |
| `rag_result` | 指标和诊断详情 |

前端不得根据多个字段自行推断不一致的状态。

## 9. 可靠性要求

1. Redis 消息只有在任务写库成功后才能 ACK。
2. 进程启动时重新载入 `pending` 和中断的 `processing` 任务。
3. 每个阶段单独记录重试次数和错误。
4. Parse、Index、Memory、Callback 分别重试。
5. 旧 `content_version` 的任务不能覆盖新版本结果。
6. 任何阶段都不能删除或覆盖新版本已经写入的数据。
7. Knowledge 的 `rag_status` 更新必须携带任务 ID、内容版本和 ACL 版本。

## 10. MVP 边界

MVP 只完成以下内容：

1. 单 RAG Worker 进程内的多 Lane 调度。
2. 任务拆分为正文和附件。
3. 任务状态持久化与进程重启恢复。
4. Parse 与 Index 分离。
5. 索引完成后立即可搜索。
6. Knowledge 统一展示 `rag_status`。
7. Index Lane 写入确定性 `branch_keys` 和受控 Entity 精确匹配。
8. Memory Lane 承载候选 Entity、合并建议和后续增强。

暂不完成：

1. 多进程部署。
2. 独立的 Asynq、Kafka 或其他外部任务框架。
3. 候选 Entity 的自动合并、Fact、摘要和语义树节点的实现。
4. 复杂的前端进度百分比。
5. 跨服务的父任务表和详细子任务状态展示协议。

## 11. 实施顺序

1. 增加任务子资源字段和统一状态枚举。
2. 将事件消费改为 Dispatcher + 任务落库。
3. 实现有界 Lane 队列和进程内 Worker 池。
4. 将现有解析、切块逻辑迁入 Parse Lane。
5. 将 Embedding 和 ES 写入迁入 Index Lane。
6. 在 Index Lane 写入确定性的元数据、受控 Entity 分支和 `branch_keys`。
7. 在 Index 成功后提前回调 `ready`。
8. Memory Lane 先接候选 Entity 处理器，后续补 Fact 和摘要逻辑。
9. 增加进程重启后的任务恢复。
10. 增加并发、批次、模型调用上限配置。

## 12. 待实现时再定的参数

以下参数不阻塞链路设计，但实现前需要确定：

1. 每个 Lane 的队列长度和线程数。
2. 单任务 Embedding 批次大小和并发数。
3. Embedding 模型的全局并发上限。
4. Parse 外部服务的并发上限。
5. 重试次数、退避时间和任务超时。
6. `ready` 提前回调后，Knowledge 是否需要保留 Memory 的独立内部状态。
