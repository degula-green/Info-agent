# RAG 服务分阶段重构计划

> 状态：讨论基线
>
> 范围：`services/rag`
>
> 目标：固定 MVP 范围，分阶段替换旧链路，避免字段持续扩张和长期兼容旧写法。

## 1. 文档目的

本文只定义服务三的重构阶段、阶段边界和待讨论事项，不直接规定最终实现细节。

本计划与以下设计文档配套：

- `consumer-pipeline-optimization.md`
- `树形rag结构mvp方案.md`

### 1.1 核心原则

1. 先冻结契约，再实现代码。
2. 每个阶段只解决本阶段的问题，不提前引入后续能力。
3. RAG 数据可以从 Knowledge 回放重建，不以旧 RAG 表为权威来源。
4. 不进行新旧存储双写。
5. API 兼容只放在接口适配层，不进入核心存储和业务逻辑。
6. 旧 `memory_*` 写链路不再扩展，不再增加新的兼容分支。
7. 传统 RAG 是主链路，树是后续导航索引。
8. Fact、Entity Tree 语义增强和 KV 不属于 MVP。

### 1.2 阶段总览

```text
Phase 0：冻结旧链路
Phase 1：数据、事件和任务契约
Phase 2：消费者 Lane 与状态链路
Phase 3：传统 RAG 基线
Phase 4：权限接入
Phase 5：树形 RAG MVP
Phase 6：树检索影子与灰度启用
Phase 7：重建、切换和旧链路清理
Phase 8：Fact、Entity Tree 语义增强和 KV
```

MVP 覆盖 Phase 0 至 Phase 7，Phase 8 明确后置。

## 2. Phase 0：冻结旧链路

### 2.1 目标

停止继续扩展旧结构，避免重构过程中再次形成新旧并存、双写和兼容分支。

### 2.2 本阶段冻结

- 旧 `memory_*` 主链路不再增加新能力。
- 不再用旧 Fact、Node、NodeSource 关系承载新的树逻辑。
- 不再通过修改旧字段的含义兼容新设计。
- 不做新旧表双写。
- 所有新增设计先进入契约文档，再进入实现。

### 2.3 交付物

- 旧链路清单。
- 旧字段和旧表用途说明。
- 停止扩展的模块清单。
- 可以从 Knowledge 重建的数据范围。

### 2.4 完成标准

- 新功能不再修改旧树写入逻辑。
- Repository 不再新增兼容旧表和新表的分支。
- 文档明确区分旧链路和目标链路。

## 3. Phase 1：数据、事件和任务契约

### 3.1 目标

冻结 RAG 服务最基础的数据契约，为状态、权限、索引和树提供稳定身份。

### 3.2 本阶段冻结

```text
knowledge.ready 事件结构
一个 KnowledgeItem = 一个资源 = 一个普通处理任务
resource_type 和 resource_id 定义
content_version、acl_version、content_hash 规则
processing_jobs 字段
rag_status 状态机
metadata_only 的处理
旧 succeeded 的清理方式
```

### 3.3 已确认契约

#### 3.3.1 资源和 KnowledgeItem 边界

- 一次采集只产生一个可处理资源：一条消息或一个附件，不存在“一条消息携带多个附件”的资源聚合场景。
- 一个 KnowledgeItem 只对应一个可处理资源，不聚合多个附件，不要求 RAG 再做子资源拆分。
- `resource_type` 只有两个合法值：

```text
message
attachment
```

- `resource_id` 的对应关系固定为：

```text
message    -> message_id
attachment -> attachment_id
```

- 附件具体类型不进入 `resource_type`。格式和业务分类分别由回源数据中的 `mime_type`、`content_type`、`file_name` 表达。
- 不允许设置 `resource_type=pdf/docx/image/spreadsheet` 等格式值。

#### 3.3.2 `knowledge.ready` 事件结构

一个 `knowledge.ready` 只对应一个 KnowledgeItem 和一个可处理资源。事件 payload 固定为：

```text
resource_type
resource_id
knowledge_item_id
source_conversation_id
source_conversation_type
source_audience_policy
content_version
acl_version
content_hash
content_variant
content_access_required
```

约束如下：

- 事件不携带正文、附件数组、文件内容和完整业务元数据。
- 正文、scope、ownership、附件详情和具体类型由 RAG 通过 Knowledge 内部 HTTP 接口回源。
- `source_conversation_id` 是 Knowledge 内部会话 UUID，不是飞书、微信或企业微信外部会话 ID。
- `source_conversation_type` 取值为 `group`、`private` 或空。
- `source_audience_policy` 取值为 `source_conversation_members`、`organization_members`、`owner_only`。
- 平台会话采集内容必须提供内部 `source_conversation_id`。
- 外部平台会话 ID 由 RAG 回源作为展示和来源定位数据，不能用于权限判断。
- `content_variant` 一期只使用 `display`，为后续原文变体保留 `original`。
- `content_access_required` 表示该资源内容是否受保护，不改变基础任务拆分规则。
- 一个事件对应一个任务，不允许一个事件展开成多个资源任务。

`knowledge.ready` 的发布语义固定为：

- Knowledge 已经保存稳定的资源快照。
- Knowledge 已经确定归属和资源身份。
- Knowledge 已经确认内容和权限登记的版本。
- 该事件不保证资源一定可以解析出正文。
- 资源是否能检索、是否 `metadata_only`、是否失败由 RAG 处理和回调。

Service2/Knowledge 的消息队列投递侧必须同步修改：

- `knowledge.outbox_events.payload` 增加上述会话和受众字段。
- Redis Stream 中的 `knowledge.ready` envelope 必须携带同样字段。
- `source_conversation_id` 必须来自 `conversation_ingestions.id`，不能使用外部会话 ID。
- 附件事件从所属消息或会话继承 `source_conversation_id` 和受众策略。
- 私人共享到组织时使用 `source_audience_policy=organization_members`，来源会话 ID 只保留为审计信息。
- RAG 回源后必须校验事件中的内部会话 ID 与 Knowledge 权威记录一致。

#### 3.3.3 版本和内容身份

- `content_version` 由 Knowledge 维护，资源内容变化时递增。
- `acl_version` 由 Knowledge/Core 协作维护，只表示权限关系版本变化。
- `content_hash` 是资源权威内容的规范 SHA-256，用于校验回源快照。
- `processing_version` 是 RAG 内部处理规则指纹，用于解析器、Chunking、Embedding 或索引映射发生变化后的重建。
- RAG 配置或模型升级不自动触发普通事件重处理。需要重建时，由显式 `reindex` 任务携带新的 `processing_version`。
- RAG 回源后发现 `content_version` 或 `content_hash` 与事件不一致时，不允许继续写入当前任务结果。

#### 3.3.4 任务身份和幂等

事件消费幂等和任务身份分开定义：

```text
事件幂等：
source_event_id 唯一

普通处理任务：
knowledge_item_id
+ resource_type
+ resource_id
+ content_version

显式重建任务：
在普通任务身份上增加 job_type=reindex 和 processing_version
```

规则如下：

- 重复投递同一个 `source_event_id` 只能命中同一个任务。
- 同一个 `source_event_id` 携带不同 payload 时必须拒绝，不能覆盖原任务。
- 普通采集事件不因 RAG 配置变化自动创建重处理任务。
- 一个任务只对应一个 KnowledgeItem 和一个可处理资源。
- 终态任务保留，用于审计、排障和重建追踪，不直接删除。
- `processing_jobs` 至少增加 `resource_type` 和 `resource_id`；`processing_version` 作为重建任务身份和排障字段使用。

#### 3.3.5 对外状态机

前端唯一依赖的 RAG 状态是 `knowledge_items.rag_status`，取值固定为：

```text
pending
processing
ready
metadata_only
failed
cancelled
```

状态语义如下：

| 状态 | 含义 |
|---|---|
| `pending` | RAG 已接收事件，任务尚未开始 |
| `processing` | 正在回源、解析、切块、Embedding 或建立基础索引 |
| `ready` | 基础 Chunk、Embedding 和 ES 索引已经可搜索和问答 |
| `metadata_only` | 元数据已保存，但没有可检索正文 |
| `failed` | 基础索引无法完成 |
| `cancelled` | 保留给后续删除、取消或版本失效场景，MVP 不产生该状态 |

状态流转固定为：

```text
pending -> processing -> ready
pending -> processing -> metadata_only
pending -> processing -> failed
pending -> cancelled
processing -> cancelled
```

- `ready` 是基础检索链路的成功终态，不等待 Memory。
- Memory 成功或失败都不能改变 `ready`。
- `processing_status` 继续表示 Knowledge 自身的处理状态，不表示 RAG 是否可搜索。
- 前端不得依赖 `processing_status`、`rag_job_id`、`rag_finished_at` 或 `rag_result` 拼接 RAG 列表状态。
- 旧 `succeeded` 不做长期兼容。数据需要保留时做一次性迁移，否则直接清理。

#### 3.3.6 `metadata_only`

`metadata_only` 是独立成功终态：

- 不生成 RAG Chunk。
- 不生成 Embedding。
- 不生成 `branch_keys`。
- 不进入 Chunk 检索索引。
- 文件元数据和对象引用继续由 Knowledge 管理。
- 文件目录、文件名和元数据可通过 Knowledge 或独立元数据接口发现。

判定规则：

- 文件格式明确不支持，或解析结果没有可检索正文时，进入 `metadata_only`。
- 外部解析服务暂时不可用、网络失败或任务执行异常时，进入 `failed` 并按策略重试。
- `metadata_only` 不能作为“解析失败的委婉状态”。

#### 3.3.7 `ready` 的提交语义

`ready` 的准确含义是：

> 基础 Chunk、Embedding 和 Elasticsearch 索引已经完成，内容可以被搜索和问答使用。

进入 `ready` 前至少完成：

1. Chunk 已写入 RAG PostgreSQL。
2. Embedding 已完成。
3. Elasticsearch bulk 写入成功。
4. 索引 refresh 策略保证随后搜索可以查到当前版本。
5. RAG Outbox 回调记录已经持久化。

Memory Lane 不属于 `ready` 的前置条件。

#### 3.3.8 状态回调

Knowledge 的 `knowledge.ready` 事件只表示上游资源可以交给 RAG 处理，不表示内容已经可搜索。

状态回调链路固定为：

```text
RAG 处理完成
  -> rag.outbox_events
  -> Callback Lane
  -> POST /internal/knowledge/{knowledge_item_id}/rag-result
  -> knowledge.knowledge_items
```

Callback 更新以下字段：

```text
rag_status
rag_source_event_id
rag_job_id
rag_content_version
rag_acl_version
rag_started_at
rag_finished_at
rag_last_error
rag_result
```

职责边界如下：

- `knowledge.outbox_events` 只负责投递 `knowledge.ready`；`published_at` 不等于 RAG 已处理完成。
- `rag.outbox_events` 只负责将 RAG 结果可靠投递给 Knowledge。
- `processing_jobs.status` 是 RAG 内部调度状态，不作为前端状态来源。
- `knowledge_items.rag_status` 是前端唯一 RAG 状态来源。
- Callback 必须携带任务 ID、资源身份、内容版本和 ACL 版本。

#### 3.3.9 删除、取消和 Memory 状态边界

MVP 不实现以下操作：

- 用户主动取消 RAG 任务。
- 跨服务删除 KnowledgeItem、消息或附件。
- 附件失效和资源过期。
- 因内容编辑产生的历史版本取消。

`cancelled` 只作为保留枚举存在，MVP 没有外部触发接口。

Memory Lane 在 Phase 1 不引入 `memory_status`，也不复用旧 `memory_*` 表承载基础任务状态。Memory Lane 的实现和独立状态模型在后续阶段单独定义。

#### 3.3.10 数据库和 Elasticsearch 重构边界

RAG 自有 PostgreSQL 表和 Elasticsearch 索引由 RAG 服务负责，可以按新契约重新设计，不以旧字段兼容为约束。

规则如下：

- 不需要为旧 `memory_*` 表和旧字段保留兼容写入。
- 与 MVP 无关的旧表、旧字段和旧索引可以直接删除。
- 需要保留的数据必须能够从 Knowledge 回放重建。
- 不进行新旧存储双写。
- API 兼容只允许存在于接口适配层，不能污染核心任务、状态和索引结构。
- RAG 自有表名、字段名和 ES mapping 可以随阶段设计调整。

### 3.4 完成标准

- 一个 KnowledgeItem 只对应一个可处理资源和一个普通任务。
- 事件只表达一个资源，`resource_type`、`resource_id` 和版本身份唯一。
- 平台会话来源事件携带内部 `source_conversation_id`、`source_conversation_type` 和 `source_audience_policy`。
- RAG 回源校验事件中的内部会话 ID 与 Knowledge 权威记录一致。
- `rag_status` 成为前端唯一 RAG 状态来源。
- `processing_jobs` 保存资源和任务最小身份，普通任务与显式重建任务边界清楚。
- `ready`、`metadata_only`、`failed` 的判定没有歧义。
- Callback 只通过 `rag.outbox_events` 回写 `knowledge.knowledge_items`。
- MVP 不包含删除、取消和 Memory 状态。
- 旧 RAG 表、字段和 ES 索引可以无兼容负担地删除或重建。

### 3.5 待实现参数

以下参数不改变 Phase 1 契约，在实现阶段确定：

- `content_hash` 对文字消息和附件字节的具体规范化算法。
- `processing_version` 的组合字段和字符串格式。
- `reindex` 任务的触发接口、批量范围和并发上限。
- `metadata_only` 文件在 Knowledge 侧的具体发现接口。
- 旧数据的迁移、归档或直接清理范围。

## 4. Phase 2：消费者 Lane 与状态链路

### 4.1 目标

实现事件消费、任务拆分、独立阶段执行和可靠状态回写。

### 4.2 固定结构

```text
Dispatcher
Parse Lane
Index Lane
Memory Lane
Callback Lane
```

### 4.3 已确认契约

#### 4.3.1 进程和 Lane 模型

Phase 2 保持单进程、多逻辑 Lane：

```text
一个 RAG Worker 进程
├── Dispatcher
├── Parse Lane
├── Index Lane
├── Memory Lane
└── Callback Lane
```

约束如下：

- 每个 Lane 使用独立线程池和有界内存队列。
- PostgreSQL 是任务身份和任务状态的权威来源。
- 内存队列只是加速器，不是不可丢失的队列。
- MVP 不引入 Celery、Asynq、Kafka 或独立 Worker 进程。
- 任务表和租约模型必须支持未来拆成多个 Worker 进程。

#### 4.3.2 Dispatcher 的 ACK 边界

Dispatcher 只负责校验、消费幂等和创建任务，不执行解析、Embedding 或模型调用。

事件处理顺序固定为：

```text
校验事件
  -> 写 processing_jobs
  -> 提交 PostgreSQL
  -> ACK Redis
  -> 投递内存 Lane
```

- 不能在 PostgreSQL 提交前 ACK。
- 不需要等待整条任务完成后再 ACK。
- 内存队列投递失败不影响 ACK，因为任务已经持久化，可以由恢复扫描重新装载。
- 重复投递同一个 `source_event_id` 时直接 ACK，不重复创建任务或入队。
- 同一个 `source_event_id` 携带不同 payload 时必须拒绝。

#### 4.3.3 任务行和执行记录

普通任务只有一条 `processing_jobs` 记录，不为每个 Lane 创建独立主任务。

主任务行负责保存当前事实：

```text
resource_type
resource_id
status
current_stage
lane
lease_owner
lease_until
retry_count
next_retry_at
last_error
```

增加追加型 `processing_job_attempts` 表记录阶段执行：

```text
job_id
stage
attempt
started_at
finished_at
retryable
error_code
error_message
```

- `processing_jobs` 保存当前状态，供调度和恢复使用。
- `processing_job_attempts` 保存执行历史，供重试统计、排障和审计使用。
- 不把 Parse Job、Index Job、Callback Job 拆成多个互相竞争的主任务。

#### 4.3.4 Lane 之间的数据交接

Parse Lane 必须在进入 Index Lane 前把 Chunk 持久化到 RAG PostgreSQL。

固定流转：

```text
Parse Lane
  -> fetch
  -> parse
  -> chunk
  -> 写入 Chunk，embedding_status=pending
  -> current_stage=index
  -> 投递 Index Lane

Index Lane
  -> 读取已持久化 Chunk
  -> Embedding
  -> 写入 Elasticsearch
  -> 更新 embedding_status
  -> status=ready
```

- Index Lane 崩溃后只重做 Embedding 和索引，不重新下载、解析或切块。
- Parse Lane 不通过进程内对象直接交接最终 Chunk。
- Lane 队列只传递任务身份，不传递大体积正文或向量。

#### 4.3.5 阶段级重试

重试以阶段为单位，不从头重做已经成功的阶段。

```text
来源下载失败
  -> 重试 Parse 阶段

解析成功但 Embedding 失败
  -> 重试 Index 阶段，不重新 Parse

Embedding 成功但 ES 写入失败
  -> 重试索引写入，不重新 Embedding
```

- 每个阶段记录独立重试次数和错误。
- `ready` 后不再自动重试，只能通过显式 `reindex` 任务重处理。
- 明确的不可解析格式直接进入 `metadata_only`，不通过重试制造成功。
- 外部解析服务暂时不可用、网络超时或索引暂时不可用时进入可重试失败。
- 超过重试上限后进入 `failed`，不能无限占用调度资源。

#### 4.3.6 租约和崩溃恢复

任务抢占和恢复依靠 PostgreSQL，不依赖内存队列。

```text
lease_owner
lease_until
```

规则如下：

- Worker 启动时扫描 `pending` 和租约过期的 `processing` 任务。
- 通过 `FOR UPDATE SKIP LOCKED` 抢占任务。
- 获取租约后更新 `lease_owner` 和 `lease_until`。
- 长任务必须定期续租。
- 进程退出后租约到期，任务可以由新 Worker 重新领取。
- 根据 `current_stage` 从最近的安全阶段继续执行。
- 同一任务不能同时被两个 Worker 有效执行。

#### 4.3.7 状态写入职责

每个 Lane 只能修改自己负责的状态和字段：

| Lane | 状态职责 |
|---|---|
| Dispatcher | 创建任务，设置 `pending` |
| Parse Lane | 设置 `processing`，更新来源、解析和 Chunk 信息，切换到 `index` |
| Index Lane | 完成基础索引后设置 `ready` 或 `metadata_only`，不可恢复时设置 `failed` |
| Memory Lane | 不修改基础任务状态 |
| Callback Lane | 不修改业务状态，只发送 RAG Outbox |

`ready`、`metadata_only` 和 `failed` 只能由基础处理链路产生。Memory 和 Callback 不能改变这些业务状态。

#### 4.3.8 状态回调和 Outbox

Callback 只上报对前端有意义的业务状态：

```text
开始基础处理 -> processing
基础处理完成 -> ready | metadata_only | failed
Memory 完成 -> 不回写基础 rag_status
```

不回调 `fetch_done`、`parse_done`、`chunk_done`、`embed_done` 等内部阶段。这些阶段只记录在 RAG 任务和 attempt 表中。

每次状态变更和对应 `rag.outbox_events` 记录必须写入同一个 PostgreSQL 事务。

Callback Lane 负责：

- 读取 `rag.outbox_events`；
- 调用 Knowledge 的状态回调接口；
- 成功后标记 Outbox 为已发布；
- 失败后按退避策略重试；
- 不重新计算任务状态。

#### 4.3.9 Memory Lane 的 MVP 范围

Phase 2 只实现 Memory Lane 的边界和调度能力，不实现候选 Entity、Fact、摘要或语义树。

- Memory Lane 默认处理器可以配置为 no-op。
- Memory 只接收已经 `ready` 的任务。
- Memory 失败不能改变基础任务状态。
- Memory 不阻塞搜索、问答和前端 `ready` 展示。
- 真正处理器从 Phase 5 开始接入。

#### 4.3.10 顺序和版本并发

系统不依赖 Redis 投递顺序或 Lane 执行顺序。

- 同一资源同一内容版本只允许一个未终态普通任务。
- 同一个 KnowledgeItem 不能在多个 Lane 中并行处理。
- `message` 和 `attachment` 属于不同资源，可以并发处理。
- Callback 必须校验 `content_version` 和 `acl_version`。
- 旧任务的终态回调不能覆盖新版本状态。
- 新任务不能覆盖旧版本已经成功写入的 Chunk 和 ES 文档。

Phase 1 已确认 MVP 不实现删除和取消，因此当前不主动打断新旧版本竞争，只通过版本守卫阻止旧结果覆盖新结果。

#### 4.3.11 队列满和背压

PostgreSQL 是任务队列，内存队列只用于提高吞吐。

内存队列满时：

- 任务继续保持 `pending` 或可恢复状态；
- 不阻塞 Dispatcher 消费其他事件；
- 不删除数据库任务；
- 由恢复扫描或下一轮调度重新装载；
- 已经持久化的任务可以 ACK Redis。

#### 4.3.12 关闭和恢复

进程正常关闭时：

- 停止 Dispatcher 接收新任务；
- 等待 Lane 在超时时间内完成当前阶段；
- 未完成任务的租约可以主动释放，也可以等待过期；
- 不依赖进程内队列保存未完成任务；
- 重启后从 PostgreSQL 恢复 `pending` 和中断的 `processing` 任务。

外部解析和模型调用不能保证即时取消，因此每个阶段必须使用幂等写入和版本守卫。

### 4.4 完成标准

- 大型附件不阻塞消息任务，单个任务失败不影响其他任务。
- 任务在 PostgreSQL 中先持久化，Redis 消息只在持久化后 ACK。
- Parse 和 Index 分开执行，Chunk 先持久化再进入 Index Lane。
- 任一步骤崩溃都可以从安全阶段恢复，不重复生成最终数据。
- 同一任务不会被两个 Worker 同时有效执行。
- 基础索引完成后立即进入 `ready`，不等待 Memory。
- 状态回调只在 `processing` 和终态发生。
- 内存队列满不会造成任务丢失。
- Memory Lane 不阻塞基础 RAG。

### 4.5 验收场景

至少覆盖以下故障点：

1. 写库成功、ACK 前崩溃。
2. ACK 成功、投递内存队列前崩溃。
3. Parse 中途崩溃。
4. Chunk 已落库、Index 尚未开始。
5. Embedding 完成、ES 写入前崩溃。
6. ES 写入成功、Callback 尚未发送。
7. Callback 重复投递。
8. 同一事件重复消费。
9. 两个消费者同时争抢同一任务。
10. 进程正常关闭时正在处理长附件。

每个场景都必须满足：不丢任务、不重复生成最终数据、可以恢复到正确阶段、`ready` 不会提前回调。

### 4.6 待实现参数

以下参数不改变 Phase 2 契约，在实现阶段确定：

- 每个 Lane 的队列长度和线程数。
- 单任务 Embedding 批次大小和并发数。
- Embedding 模型的全局并发上限。
- 外部解析服务的并发上限。
- 各阶段重试次数、退避时间、租约时长和任务超时。
- Memory Lane 默认处理器的开关和上线时间。

## 5. Phase 3：传统 RAG 基线

### 5.1 目标

在不依赖树和 Fact 的情况下，完成可独立运行的传统 RAG。

### 5.2 目标链路

```text
Source
  -> Chunk
  -> PostgreSQL
  -> Elasticsearch
  -> BM25 + kNN + RRF
  -> 可选 Rerank
```

### 5.3 本阶段冻结

```text
Chunk 身份和主存结构
ContextHeader
PostgreSQL 与 Elasticsearch 的权威关系
Elasticsearch Chunk 文档结构
Embedding 合同
BM25、kNN 和 RRF 的基础行为
Rerank 默认策略
三个搜索入口的行为
引用、上下文和去重格式
检索诊断字段
首次搜索评测集和验收指标
```

### 5.4 已确认契约

#### 5.4.1 Chunk 身份和主存结构

Chunk 是传统 RAG 的统一检索证据单元。`chunk_id` 使用确定性 ID，不直接使用数据库随机 UUID：

```text
sha256(
  knowledge_item_id
  + resource_type
  + resource_id
  + content_version
  + processing_version
  + chunking_version
  + chunk_index
)
```

Chunk 主记录至少包含：

```text
chunk_id
knowledge_item_id
resource_type
resource_id
content
content_hash
content_version
processing_version
chunking_version
chunk_index
chunk_count
sent_at
conversation_id
document_id
scope_key
ACL 相关字段
source_locator
branch_keys
embedding_status
lifecycle_status
created_at
updated_at
```

约束如下：

- 同一来源版本和处理版本重复处理时，`chunk_id` 必须稳定。
- Chunk 顺序由 `chunk_index` 定义。
- Chunk 必须有来源定位，能够回到消息、附件、页、段落、表格或工作表位置。
- 支持展示正文的 Chunk 才能生成 Embedding 和进入向量召回。
- `branch_keys` 在 Phase 3 写空数组，Phase 5 再写入正式分支。

#### 5.4.2 不使用父子 Chunk

MVP 不引入 parent-child chunk。

原因如下：

- 父子 Chunk 会增加权限、版本、去重和上下文组装复杂度。
- 当前可以通过结构化切块、Chunk 重叠和邻近 Chunk 扩展满足上下文需求。
- 评测证明有必要后，再增加 `parent_chunk_id`，不在 Phase 3 提前引入。

#### 5.4.3 ContextHeader

标题、文件名、章节路径等结构信息单独保存，不直接混入 Chunk 证据正文。

建议字段：

```text
title
file_name
heading_path
source_kind
```

使用规则：

- Embedding 输入使用 `ContextHeader + Chunk 正文`。
- BM25 分别索引 `content`、`title`、`file_name` 和 `heading_path`。
- 展示和引用使用原始 Chunk 正文。
- ContextHeader 变化必须能够通过处理版本触发重新索引。

#### 5.4.4 Chunk 切分策略

Phase 3 冻结以下原则：

- Chunk 必须可重现。
- Chunk 必须保持来源顺序和定位。
- 表格、代码、消息和普通文本使用各自的切分边界。
- 不允许跨表格无表头切分，也不允许把不同消息正文混成一个 Chunk。

初始参数作为实现配置，不作为存储契约：

```text
普通文本：400 到 600 个中文字符或等效 token
重叠：60 到 80 个中文字符
表格：按表头和行组切分
代码：按函数、类或代码块切分
消息：一条消息至少一个 Chunk，长消息再切
```

具体参数由评测集确定。

#### 5.4.5 PostgreSQL 与 Elasticsearch 的权威关系

- PostgreSQL 是 Chunk、处理版本和索引状态的权威来源。
- Elasticsearch 是可重建的检索投影。
- 原始消息、附件和原始文件继续以 Knowledge/MinIO 为权威来源。
- Parse Lane 先把 Chunk 写入 PostgreSQL，`embedding_status=pending`。
- Index Lane 完成 Embedding 和 ES 写入后，将状态更新为 `ready`。
- Elasticsearch 丢失时可以从 PostgreSQL Chunk 重建。
- 解析或切块规则改变时，从 Knowledge 重新处理并产生新的 `processing_version`。
- 不允许把 Elasticsearch 作为唯一 Chunk 正文存储。

#### 5.4.6 Elasticsearch Chunk 文档

一个 Chunk 对应一个 Elasticsearch 文档，同一个文档同时支持正文检索、向量检索和过滤。

冻结字段：

```text
chunk_id
knowledge_item_id
resource_type
resource_id
scope_key
title
file_name
heading_path
content
embedding
content_hash
content_version
processing_version
chunking_version
chunk_index
chunk_count
conversation_id
document_id
sent_at
sender_display_name
source_locator
ACL 相关字段
lifecycle_status
branch_keys
```

规则如下：

- `content` 用于 BM25。
- `embedding` 用于 kNN。
- `branch_keys` 在 Phase 3 写空数组，为 Phase 5 预留 mapping。
- ACL 和生命周期字段只用于过滤，不参与相关性评分。
- `source_locator` 使用固定子字段，禁止动态写入任意对象。
- Mapping 使用 `dynamic: strict`，新增字段必须显式修改 mapping。

#### 5.4.7 Embedding 合同

- 一个物理索引只使用一个 Embedding 模型和一组固定维度。
- MVP 使用 `text-embedding-v4` 和 1536 维。
- 相似度使用 `cosine`。
- 输入文本为 `ContextHeader + Chunk 正文`。
- 不使用多向量、ColBERT 或稀疏向量。
- Embedding 模型名称和维度属于 `processing_version` 组成部分。
- 批次大小、任务并发和全局模型并发是配置项，不是存储契约。
- Embedding 失败只重试 Index Lane，不重新解析和切块。

#### 5.4.8 BM25、kNN 和 RRF

基础检索行为固定为：

1. BM25 和 kNN 并行执行。
2. 权限、Scope、生命周期和版本条件放入 `filter`。
3. BM25 和 kNN 结果分别保留原始排名。
4. 使用 RRF 融合，不直接相加不同索引的原始分数。
5. RRF 后进行资源级去重。

初始参数：

```text
BM25 检索：24
kNN 检索：24
kNN num_candidates：96
RRF k：60
BM25 权重：0.5
向量权重：0.5
最终候选：8 到 10
```

BM25 查询字段：

```text
content
title^2
file_name^2
heading_path
```

BM25 默认使用 OR 语义，避免自然语言问题因为要求所有词同时出现而返回空结果。

#### 5.4.9 Rerank

Rerank 在 Phase 3 默认关闭，只保留配置和适配接口。

规则如下：

- 先建立不依赖 Rerank 的传统 RAG 基线。
- Rerank 只处理 RRF 后的前 8 到 12 条。
- Rerank 超时或失败时直接使用 RRF 结果。
- Rerank 不能成为搜索可用性的单点依赖。
- 是否默认启用由 Phase 3 评测结果决定。

#### 5.4.10 三个搜索入口

| 入口 | 检索方式 |
|---|---|
| AI 问答 | BM25 + kNN + RRF，可选 Rerank，然后组装 Prompt |
| 全局搜索 | BM25 + kNN + RRF，可选 Rerank，不生成回答 |
| 知识库搜索 | 仅 BM25，用于文件名、标题、编号和原文关键词 |

公共约束：

- Query Rewrite 默认关闭。
- 显式编号、日期、金额、文件名等精确查询提高 BM25 权重。
- 语义问题使用混合检索。
- 三个入口共用一套过滤构造和权限适配接口。
- 三个入口不允许各自实现一套独立权限逻辑。

#### 5.4.11 输出、引用和上下文

- 按 `resource_id` 或 `knowledge_item_id` 做最终去重。
- 单个资源默认最多返回 2 个 Chunk。
- 引用按资源聚合，而不是每个 Chunk 生成独立引用卡片。
- 引用保留 `chunk_id`、来源定位、时间和文档信息。
- AI 上下文可以按同一资源的相邻 `chunk_index` 扩展有限上下文。
- 邻近上下文扩展必须再次执行权限检查。
- 不允许通过相邻 Chunk 绕过原 Chunk 的 ACL。

#### 5.4.12 权限边界

Phase 3 只定义搜索范围内的权限接口、过滤字段和调用位置，真实 Core/OpenFGA 授权在 Phase 4 接入。

规则如下：

- Phase 3 生产路径不提供绕过授权的搜索开关。
- 受保护内容在 Phase 4 完成前保持失败关闭。
- Scope、ACL 和生命周期过滤在 Phase 3 就进入 ES 查询。
- 最终授权复核接口在 Phase 3 预留，Phase 4 接入真实实现。
- ES 或权限依赖不可用时不允许降级为未过滤查询。

#### 5.4.13 检索诊断

每次检索至少记录：

```text
entry
query_hash
resolved_scope
candidate_count
bm25_candidate_count
knn_candidate_count
authorized_count
rrf_result_count
rerank_enabled
rerank_result_count
degraded
stage_duration_ms
```

诊断日志不得记录受保护正文、Token 或完整下载 URL。

### 5.5 完成标准

- 不依赖树、Fact 或 KV，可以完成索引、搜索和问答。
- Chunk 身份稳定，可以从 PostgreSQL 重建 Elasticsearch。
- 解析、切块规则变化时可以从 Knowledge 重新处理。
- BM25、kNN、RRF 的输入、过滤和输出语义固定。
- Rerank 关闭时传统 RAG 仍然完整可用。
- 三个入口共享过滤和检索基础逻辑。
- 引用和上下文格式可以直接供前端和 AI Prompt 使用。
- 失败任务可以独立重试。

### 5.6 首次搜索评测

评测集至少覆盖：

```text
精确关键词
文件名
日期问题
会话语义
长文档语义
跨段落问题
多资源问题
无答案问题
权限隔离
```

指标至少包括：

```text
Recall@5
Recall@10
MRR
nDCG@10
zero_result_rate
p50/p95 latency
权限泄露数
```

验收规则：

- 权限泄露必须为零。
- Embedding 失败可以降级为 BM25。
- Rerank 失败可以降级为 RRF。
- Elasticsearch 或权限服务不可用时必须失败关闭。
- 不允许返回未过滤内容。

### 5.7 待实现参数

以下参数不改变 Phase 3 契约，在实现和评测阶段确定：

- Chunk 的具体字符数、重叠范围和结构边界。
- 单任务 Embedding 批次大小和全局并发上限。
- BM25、kNN、RRF 参数的最终调优值。
- Rerank 是否在特定入口或特定查询类型启用。
- 邻近上下文扩展的最大窗口和 Chunk 数。

## 6. Phase 4：权限接入

### 6.1 目标

固定服务三与 Core、Knowledge 的权限边界，保证检索和问答不绕过授权。

权限贯穿三个服务，本阶段只冻结服务三需要的接口和调用顺序。

### 6.2 本阶段边界

- RAG 不直接连接 OpenFGA。
- RAG 只调用 Core 的授权服务。
- RAG 不自行定义另一套权限模型。
- RAG 不负责敏感信息识别、脱敏和审批。
- 权限失败默认拒绝。
- 树和 KV 都不能绕过权限。

### 6.3 已确认契约

#### 6.3.1 身份、租户和 Scope 隔离

生产环境不能信任客户端直接传入的用户身份字段。建议链路：

```text
Core 签发或验证 JWT
-> RAG 验证签名、issuer 和过期时间
-> 获取 user_id
-> organization_id、Scope 和 KnowledgeBase 范围由 Core 再校验
```

- `X-User-ID` 只允许在开发和测试环境使用。
- Nginx 或网关转发时必须剥离客户端伪造的身份 Header。
- 一个搜索请求只能属于一个 Scope：

```text
scope:org:{organization_id}
scope:user:{owner_user_id}
```

- 私人知识库和组织知识库不能在一次查询中混合。
- 同一 Scope 内可以查询多个 KnowledgeBase。
- Chunk、ES 文档、树节点、任务和缓存必须携带 `scope_key`。
- 跨组织、跨用户或缺失 Scope 的查询直接拒绝或返回空。

#### 6.3.2 敏感内容的双版本投影

敏感信息识别和脱敏由 Service2/Knowledge 负责，RAG 不自行判断身份证号、手机号、API Key 或数据库配置。

一个 KnowledgeItem 仍然只有一个普通任务，但可以生成两个内容投影：

```text
display 版本：脱敏正文
protected 版本：原始正文
```

规则如下：

- display Chunk 只写入 display 索引。
- protected Chunk 只写入 protected 索引。
- 无原文权限的用户可以知道信息存在并检索脱敏版本。
- 无权限用户不能通过向量、BM25、摘要、引用或日志获得原始值。
- 原始内容不能用于生成 display 摘要。
- RAG 服务账号可以为了建立 protected 索引读取原文，但必须有独立的服务身份、版本约束和审计。
- Knowledge 需要提供受服务身份保护的索引回源接口，不能复用普通用户原文接口。
- 审批和授权关系由 Core/Knowledge 管理，RAG 只消费授权结果。

#### 6.3.3 权限资源映射

权限资源映射固定为：

| 内容 | resource_type | resource_part |
|---|---|---|
| 消息展示正文 | `knowledge_item` | `display` |
| KnowledgeItem 原始正文 | `knowledge_item` | `original` |
| 附件元数据 | `attachment` | `metadata` |
| 附件正文内容 | `attachment` | `content` |

动作规则：

- 检索候选使用 `view`。
- 问答上下文使用 `view`。
- 引用展示随上下文一起完成 `view` 检查。
- 打开或解析原文件正文使用 `view`。
- 下载原文件使用 `download`。
- RAG 不允许用 `download` 判断代替 `view`。
- 有 `download` 但没有 `view` 时，内容不能进入问答上下文。

#### 6.3.4 普通内容的粗粒度授权

不能通过 `ListObjects` 返回用户可访问的全部消息或 KnowledgeItem ID。

普通组织内容通过以下权限分区过滤：

```text
authorized_organization_ids
authorized_conversation_group_ids
private_scope_owner_user_id
```

`search-scope` 建议返回：

```text
scope_key
authorized_organization_ids
authorized_conversation_group_ids
authorized_protected_object_keys
snapshot_id
expires_at
truncated
```

规则如下：

- 组织成员授权覆盖组织内的普通 display 内容。
- 会话成员授权覆盖对应 conversation_group 的 display 内容。
- 私人内容通过 `scope:user:{owner_user_id}` 隔离。
- 不允许返回全量 KnowledgeItem 或消息 ID。
- `authorized_organization_ids` 和 `authorized_conversation_group_ids` 只包含用户实际可访问的 Scope 范围。

#### 6.3.5 protected 精确授权

敏感原文和受保护附件使用精确对象键：

```text
knowledge_original:{knowledge_item_id}
attachment_content:{attachment_id}
```

规则如下：

- protected 查询必须使用 Core 返回的授权对象集合。
- 不能先全量召回 protected 内容再到应用层过滤。
- 对象集合超出限制时 `truncated=true`。
- `truncated=true` 时受影响的 protected 分支失败关闭，不能把截断集合当成完整权限。
- 如果 protected 集合长期过大，应引入更粗的授权分区，而不是无限扩大 ES `terms`。
- 源会话成员权限由 `conversation:{source_conversation_id}` 权限分区覆盖，不需要逐条返回该会话内的原文对象键。
- 最终候选仍要执行 `check-batch`。

#### 6.3.6 源会话受众权限

来源会话受众由以下字段共同决定：

```text
source_conversation_id
source_conversation_type
source_audience_policy
```

对于从 A 群采集的内容：

```text
source_conversation_id = A 的内部 UUID
source_conversation_type = group
source_audience_policy = source_conversation_members
auth_partition_key = conversation:A 的内部 UUID
```

规则如下：

- 活跃组织成员，并且是 A 群有效参与者，可以查看 A 群来源的 display 和 protected 内容。
- 群成员身份是“平台账号已绑定内部用户”和“用户在群参与者列表中”的交集。
- 普通受限附件允许源群成员默认查看和下载。
- 加密附件允许源群成员进入查看判断，但下载仍按独立下载授权执行。
- 非源群组织成员只能查看脱敏版本，查看原文需要额外审批。
- 用户离开群聊、解绑平台账号或离开组织后，下一次查询必须失去对应权限。
- 权限变化后应失效 Scope 缓存和上下文缓存。

不在此来源会话中的组织文件库内容使用：

```text
source_audience_policy = organization_members
```

私人知识库内容使用：

```text
source_audience_policy = owner_only
```

私人共享到组织时创建新的组织副本，并使用：

```text
source_audience_policy = organization_members
auth_partition_key = org:{organization_id}
```

#### 6.3.7 权限分区与语义树

语义树分支不能直接作为 OpenFGA 权限对象。Entity 分支可能混合组织、会话、私人和受限内容。

建议新增权限分区：

```text
auth_partition_key:
  org:{organization_id}
  conversation:{conversation_id}
  user:{owner_user_id}
  protected:{resource_type}:{resource_id}
```

树结构保持：

```text
语义路径：Scope -> Domain -> Entity -> Time
权限分区：auth_partition_key
```

约束如下：

- 一个 Leaf 只能属于一个权限分区。
- 一个 Chunk 只能挂载到与其权限边界一致的 Leaf。
- 权限变化时不需要重建整个语义树，只更新权限分区或 Chunk 投影。
- 不需要为每个 Entity 节点创建 OpenFGA 关系。
- 查询时语义分支与权限分区取交集。
- 分支级检查不能替代最终资源级授权。

#### 6.3.8 display 与 protected 索引

MVP 保留独立物理索引：

```text
display Chunk 索引
protected Chunk 索引
```

两者使用相同的逻辑 Chunk 身份，但可以有不同的 mapping、保留策略和访问规则。

规则如下：

- display 索引保存普通内容和脱敏内容。
- protected 索引保存原始敏感正文和受保护附件内容。
- 普通查询不能访问 protected 索引。
- protected 查询必须同时满足 Scope、权限分区和精确对象授权。
- 索引隔离不能替代 OpenFGA 和最终复核。
- RAG 不得通过错误配置把 protected 内容写入 display 索引。

#### 6.3.9 查询权限流程

固定流程：

```text
用户身份和 Scope 校验
  -> Core 返回粗粒度权限分区
  -> Core 返回 protected 精确对象键
  -> ES display/protected 查询
  -> 候选 Chunk 执行 check-batch
  -> RRF
  -> Rerank
  -> 邻近上下文扩展
  -> 最终 check-batch
  -> 返回脱敏版本或原文
```

规则如下：

- 未授权候选不能进入 RRF 和 Rerank。
- 邻近 Chunk 必须重新执行 `check-batch`。
- Prompt 组装前必须进行最终复核。
- 复核失败时已组装上下文必须丢弃。
- 引用和日志不得包含未授权正文。

#### 6.3.10 附件权限

MVP 不对附件内部文本做自动脱敏。

| 附件类型 | 元数据 | 正文和问答 | 下载 |
|---|---|---|---|
| 普通附件 | 可见 | 组织/会话范围内按 `view` | 默认随查看权限 |
| 敏感附件 | 可见 | 需要 `attachment_content.view` | 需要 `attachment_content.download` |
| 加密附件 | 可见 | 默认失败关闭，审批后开放 | 需要单独下载授权 |
| 无法解析附件 | 可见，`metadata_only` | 不进入 Chunk 检索 | 按附件元数据权限判断 |

补充规则：

- 加密附件如果 RAG 无法在服务端安全解密，只保存元数据。
- 文件正文索引和下载授权必须分离。
- 下载 URL 不进入 RAG 上下文和引用。
- metadata-only 文件由 Knowledge 或独立元数据目录展示。

#### 6.3.11 私人知识共享到组织

私人内容共享到组织时创建新的组织侧 KnowledgeItem：

```text
private resource: scope:user:{owner_user_id}
shared resource:  scope:org:{organization_id}
access_scope: organization_members
```

规则如下：

- 只有显式共享动作才能创建组织副本。
- 普通私人内容共享后，组织成员默认可查看和下载。
- 敏感文本仍保留 display/protected 双版本，不因共享暴露原文。
- 加密或受限附件保留原有限制和审批要求。
- 私人原件和组织共享副本权限互不影响。
- MVP 不实现撤销共享。

#### 6.3.12 ACL 版本和缓存

- Core/OpenFGA 是最终授权依据。
- `acl_version` 只用于一致性校验、缓存失效和排障。
- `search-scope` 缓存不能超过 Core 返回的 `expires_at`。
- 建议 Scope 缓存最长 5 秒。
- `check-batch` 决策不能跨请求长期缓存。
- ACL 版本变化后必须失效 Scope、搜索结果和上下文缓存。
- 支持撤销事件或 ACL generation 时优先主动失效。

#### 6.3.13 权限失败和降级

- `search-scope` 不可用时 protected 分支直接为空。
- `check-batch` 不可用时受影响候选全部拒绝。
- 如果 Required 候选全部无法完成授权判断，API 返回 `503 AUTHZ_UNAVAILABLE`。
- display 只有在自身候选完成授权检查后才能返回。
- 任何依赖失败都不能降级为未过滤查询。
- 生产环境禁止使用 `AllowAllAuthorizationGateway`。

#### 6.3.14 服务身份和审计

- RAG 调 Core 使用专用服务 Token。
- Header 固定包含 `X-Caller-Service: rag`。
- 请求带 `X-Request-ID` 和 `X-Trace-ID`。
- Core 同时校验服务 Token 和调用方身份。
- RAG 读取原文用于索引时必须记录 purpose、resource_id、content_version 和 job_id。
- 日志不得包含正文、Token、完整下载 URL 或敏感字段。

#### 6.3.15 检索权限诊断

至少记录：

```text
scope_key
authorization_snapshot_id
scope_cache_hit
authorized_organization_count
authorized_conversation_count
authorized_protected_object_count
scope_truncated
candidate_check_count
candidate_denied_count
final_check_count
final_denied_count
authorization_duration_ms
degraded_reason
```

诊断字段不得包含受保护正文。

### 6.4 完成标准

- 未授权 Chunk 不进入最终上下文。
- 跨组织和跨用户数据不会出现在候选中。
- 普通内容不再通过全量 KnowledgeItem ID 列表鉴权。
- 敏感内容可以通过 display/protected 双版本安全检索。
- 源群聊成员可以默认检索和查看该群来源的 protected 内容。
- 非源群聊成员只能检索脱敏版本，除非获得额外原文授权。
- 群成员离开群聊、解绑平台账号或离开组织后，新查询立即失去对应权限。
- protected 查询不会退化为全量召回后过滤。
- 附件查看和下载权限互相独立。
- 私人共享创建独立组织副本，不影响私人原件权限。
- 树分支通过权限分区过滤，不把 Entity 当作权限对象。
- 权限逻辑集中在适配层，不散落在检索和树代码中。
- Core 不可用时不放宽过滤条件。

### 6.5 验收场景

至少覆盖：

1. 用户只能搜索自己所属的组织或私人 Scope。
2. 同一请求不能混合私人知识库和组织知识库。
3. 组织成员能够检索组织 display 内容。
4. 无原文权限的用户只能看到脱敏版本。
5. 获得审批的用户可以检索 protected 原文。
6. 权限撤销后，新查询不能返回 protected 内容。
7. `search-scope` 被截断时 protected 分支失败关闭。
8. `check-batch` 部分失败时失败项全部拒绝。
9. Core 不可用时不能返回未过滤内容。
10. 超过 100 个候选时能够正确分批复核。
11. 邻近上下文不能绕过任何 Chunk 的权限。
12. 普通附件和敏感附件的查看、下载权限正确分离。
13. 加密附件未审批前不能进入问答上下文。
14. 私人共享后组织副本可访问，私人原件权限不变。
15. Prompt、引用和最终上下文中不能出现未授权内容。
16. 源群聊成员能够默认检索和下载该群来源的普通敏感内容。
17. 非源群聊成员只能检索脱敏版本，不能默认获得 protected 原文。
18. 用户离开来源群聊或解绑平台账号后，新查询不再返回 protected 原文。
19. `knowledge.ready` 缺少或伪造内部 `source_conversation_id` 时任务失败关闭。
20. `source_audience_policy` 与 Knowledge 权威记录不一致时任务失败关闭。

### 6.6 待实现参数

以下参数不改变 Phase 4 契约，在实现阶段确定：

- JWT 的具体签名、issuer 和验签公钥管理方式。
- `search-scope` 的缓存时长、对象数量上限和截断阈值。
- `check-batch` 的批次大小和超时时间。
- display/protected 物理索引的具体命名、mapping 和保留策略。
- 敏感字段脱敏模板和原文授权审批流程。
- 附件下载审批的权限粒度和默认下载范围。
- ACL 撤销事件或 generation 的接入方式。

## 7. Phase 5：树形 RAG MVP

### 7.1 目标

建立稳定、受控、可重建的树导航数据，不让 LLM 直接决定正式树结构。

### 7.2 固定结构

```text
Scope
  -> Domain
    -> Entity
      -> Time
        -> branch_key
          -> Chunk
```

Session、Document、Time 优先作为 Chunk 元数据过滤器，不拆成独立树。

### 7.3 已确认契约

#### 7.3.1 固定 Domain

MVP Domain 固定为：

```text
organization
project
person
policy
contract
asset
location
unclassified
```

含义：

| Domain | 内容 |
|---|---|
| `organization` | 公司、部门、学校、机构 |
| `project` | 项目、活动、课程、周期性事项 |
| `person` | 内部员工、联系人或明确自然人 |
| `policy` | 制度、规范、政策 |
| `contract` | 合同、协议、订单 |
| `asset` | 系统、文档、表单、软件、数据集 |
| `location` | 办公地点、地区、位置 |
| `unclassified` | 已确认主体但暂时无法分类 |

以下类型不能作为 Domain：

```text
人事
财务
行政
技术
通知
方案
重要
普通
```

#### 7.3.2 Entity Registry 的 Scope

Entity Registry 按 Scope 隔离：

```text
organization registry: scope:org:{organization_id}
private registry:      scope:user:{owner_user_id}
```

规则如下：

- 不同组织不能共享正式 Entity。
- 私人 Entity Registry 只服务该用户。
- 私人共享到组织时使用组织 Registry 重新匹配。
- Registry 和候选实体不能跨租户合并。

#### 7.3.3 Entity ID、规范名和别名

- `entity_id` 使用不可变 UUID，创建后不因名称修改而改变。
- `canonical_name` 可以修改，`entity_id` 不变。
- 唯一键为：

```text
scope_key + domain + normalized_key
```

- Branch Key 使用 `entity_id`，不使用中文名称。
- 合并实体时保留主 Entity ID。
- 被合并 Entity 记录 `merged_into_entity_id`，不能直接删除审计信息。

名称归一化只做确定性规则：

```text
Unicode 规范化
大小写统一
全角/半角统一
空格和常见标点统一
显式别名匹配
```

MVP 不做编辑距离、包含关系或 LLM 模糊自动合并。

#### 7.3.4 正式 Entity 的来源

正式 Entity 只能来自：

1. 管理员或人工维护。
2. 明确的稳定业务标识。
3. 已绑定的内部用户、联系人 ID 或平台账号 ID。
4. 文档元数据中已经确认的主体。
5. 已确认别名。
6. 组织、部门等权威目录数据。

LLM 只能创建候选实体，不能创建正式 Entity。

#### 7.3.5 候选实体

无法确认的主体写入 `entity_candidates`：

```text
id
scope_key
candidate_name
normalized_key
candidate_domain
mention_count
distinct_source_count
first_seen_at
last_seen_at
sample_context
suggested_entity_id
status
```

规则如下：

- 候选实体不进入正式树。
- 候选实体不参与排他过滤。
- `sample_context` 只保存脱敏内容。
- 候选生成只能读取 display Chunk 或已脱敏字段。
- 候选实体按 Scope 隔离。

候选状态固定为：

```text
new
grouped
promoted
ignored
```

#### 7.3.6 候选晋升

MVP 只允许以下情况直接晋升：

1. 明确命中已有 Entity 别名。
2. 明确命中稳定业务标识。
3. 管理员或用户明确确认。
4. 权威目录已经确认。

基于提及次数、来源数量或相似度自动晋升后置。正式树优先保证精度。

#### 7.3.7 Entity Registry 的权威和重建

RAG PostgreSQL 中的 `entity_registry` 是正式 Entity 的权威数据，并支持版本化导出和导入。

规则如下：

- Chunk 和 ES 索引可以从 Knowledge 重建。
- Entity Registry 不能只依赖 Knowledge 内容自动推导。
- 人工维护、别名、稳定标识和合并关系必须独立备份。
- `registry_version` 写入 Chunk、任务和检索诊断。
- Registry 修改后必须能够触发受影响 Branch Key 的增量刷新。

如果要求所有 RAG 数据都能完全从 Knowledge 重建，则需要把 Entity Registry 迁移到 Knowledge/Core，或改成版本化配置资产。

#### 7.3.8 确定性匹配和 LLM 边界

MVP 的正式 Entity 匹配只使用确定性字典：

```text
当前 Scope 的 active canonical_name
+ active aliases
+ normalized_key
```

可以使用 Aho-Corasick、Trie 或最长匹配扫描。

正式匹配不使用 LLM，原因如下：

- 正式匹配是闭集引用解析，不是开放式生成。
- 相同 Registry 和 Chunk 必须得到相同 Branch Key。
- LLM 调用会降低索引吞吐量并增加成本。
- 查询路径再增加一次 LLM 会扩大 P95 延迟。
- LLM 故障不能阻塞基础 RAG 和索引。
- protected 原文不能被发送给外部 LLM。
- 匹配结果必须可审计、可重建和可回归测试。
- 漏匹配只会失去 Branch 加权，全库检索仍然兜底。
- 错误匹配会把内容挂到错误 Entity，代价高于漏匹配。

LLM 可以用于后续候选辅助，但不能直接决定正式结果：

```text
提出候选 Entity
建议别名
在少量候选 Entity 之间消歧
解析上下文指代并标为低置信链接
```

即使后续引入 LLM：

- 输出必须是 Registry 中的 `entity_id`。
- 低置信度只能记录，不能写入正式 Branch。
- 只能在 shadow 模式验证。
- 不能直接做排他过滤。
- 不能把 protected 原文发送给外部模型。

#### 7.3.9 Branch Key

Branch Key 格式：

```text
有月份：entity:{domain}:{entity_id}:{YYYY-MM}
无月份：entity:{domain}:{entity_id}
```

Chunk 同时保存：

```text
branch_keys
auth_partition_key
scope_key
registry_version
```

Branch Key 只用于语义导航，不代表权限。

每个 Chunk 最多写入 8 个正式 Branch：

- 按匹配可靠度和名称长度排序。
- 超出上限时记录诊断。
- 不阻止 Chunk 进入 `ready`。
- 候选 Entity 不占正式 Branch 名额。

#### 7.3.10 Tree Node

MVP 不需要 Node 向量和 Node ES 语义检索。

规则如下：

- PostgreSQL `tree_nodes` 是导航目录权威。
- Node 保存 Scope、Domain、Entity、Time、Branch Key 和统计。
- Node 不保存 Chunk 正文、Chunk ID 大数组或 Embedding。
- ES Chunk 保存 `branch_keys`。
- 正式检索只对 Chunk 执行 BM25 和 kNN。
- Node 向量、摘要和 ANN 放到 Phase 8。

#### 7.3.11 Branch Key 写入和刷新

Index Lane 在写 ES Chunk 前完成确定性 Entity 匹配，Branch Key 与 Chunk 一次写入。

规则如下：

- Entity 匹配失败不阻止 Chunk 进入 `ready`。
- Registry 新增别名、合并、停用或修改后创建 `branch_refresh` 任务。
- 只刷新受影响 Chunk 的 `branch_keys`。
- 不重建 Chunk、Embedding 和正文索引。
- Branch 刷新不增加 `content_version`，增加 `registry_version`。

#### 7.3.12 查询侧实体解析

查询流程：

```text
确定 Scope
-> 加载该 Scope 的 Entity Registry
-> 对问题执行确定性别名匹配
-> 得到 entity_ids
-> 转换为 branch_keys
-> 分支检索
-> 全库检索
-> RRF 融合
```

规则如下：

- 未命中 Entity 时不施加树过滤，直接走传统 RAG。
- 命中多个 Entity 时使用 Branch Key 集合加权。
- Entity 歧义时不做排他过滤。
- MVP 不调用 Query LLM 做正式 Entity 解析。
- 私人 Scope 只加载私人 Registry。

#### 7.3.13 权限集成

- 每次 Branch 查询必须同时带 `scope_key` 和 `auth_partition_key`。
- Branch Key 不能替代 Phase 4 权限。
- 同一 Branch 下不同权限内容必须位于不同 Leaf 和权限分区。
- 候选 Entity 不进入正式树查询。
- 跨 Scope 分支不能合并。

### 7.4 完成标准

- Domain、Entity Registry 和 Branch Key 格式稳定。
- 正式 Entity 只来自受控 Registry。
- 候选 Entity 不进入正式树。
- LLM 不能直接创建正式 Entity 或决定正式 Branch。
- Branch Key 可重现、可审计、可增量刷新。
- 未命中 Entity 时不影响传统 RAG。
- Tree Node 不保存正文和 Embedding。
- 树分支与权限分区正交。

### 7.5 验收场景

至少覆盖：

1. 同一 Chunk 可以挂多个 Branch Key。
2. 别名归并到同一个 Entity ID。
3. 候选 Entity 不进入正式树。
4. Registry 变更后能够增量刷新 Branch Key。
5. 未命中 Entity 时自动降级传统 RAG。
6. 跨 Scope 泄露为零。
7. 源群聊权限与 Entity Branch 组合后不越权。
8. 树选错时全库检索仍能返回正确 Chunk。
9. 相同 Registry 和 Chunk 重复处理得到相同 Branch Key。
10. protected 原文不进入候选实体样本和日志。

### 7.6 固定原则

- LLM 只能提出候选。
- LLM 不能直接创建正式 Entity。
- LLM 不能直接决定正式 Branch Key。
- 未知实体只进入候选表。
- 候选实体不参与正式树和排他过滤。
- 树节点不保存 Chunk 正文和 Embedding。
- 全库 RAG 始终是树检索的兜底通道。

### 7.7 待实现参数

以下参数不改变 Phase 5 契约，在实现和评测阶段确定：

- Alias Trie 或 Aho-Corasick 的具体实现。
- 单个 Chunk 的 Branch Key 排序细节。
- `branch_refresh` 的批次大小和调度方式。
- Registry 导入导出的格式和版本策略。
- 候选实体的保留时间和人工审核入口。
- Entity Registry 最终由 RAG、Knowledge 还是 Core 持有。

## 8. Phase 6：树检索影子与灰度启用

### 8.1 目标

让树逐步参与检索，但不立即承担排他性过滤责任。

### 8.2 全局运行模式

```text
off
shadow
boost
```

MVP 不启用：

```text
filter
```

Tree Mode 只使用一个全局配置项：

```text
RAG_TREE_MODE=off | shadow | boost
```

规则如下：

- 不区分组织。
- 不区分知识库。
- 不提供普通用户切换。
- 不提供前端模式选择页面。
- MVP 不引入组织级或知识库级策略表。
- 配置修改通过环境变量或部署配置完成。
- 配置变更后重启 RAG 服务生效，不做热更新。
- `filter` 不是合法配置值。

### 8.3 模式语义

#### 8.3.1 `off`

- 不加载 Entity Registry 进行查询解析。
- 不计算 Branch。
- 不执行 Branch 检索。
- 只执行传统 RAG。
- 作为全局紧急回滚开关。

#### 8.3.2 `shadow`

执行 Branch 解析和检索，但 Branch 不参与最终结果：

- 传统 RAG 结果保持与 `off` 一致。
- Branch 和全库检索并行执行。
- Branch 超时、失败或为空不能影响传统 RAG。
- 记录 Branch 命中率、错误分支率、候选缩减和延迟。
- 可按全局采样率减少 Shadow 开销。

建议配置：

```text
RAG_TREE_SHADOW_SAMPLE_RATE=1.0
```

`shadow` 是默认上线模式。

#### 8.3.3 `boost`

Branch 和全库检索同时执行，通过 RRF 融合：

- Branch 只增加 RRF 票数，不排除全库结果。
- 全库检索始终保留。
- 同一个 Chunk 同时命中 Branch 和全库时，RRF 票数累加。
- 未命中 Entity 时直接退回传统 RAG。
- 不允许 Branch 成为唯一召回通道。

建议配置：

```text
RAG_TREE_BRANCH_WEIGHT=0.5
```

全库 BM25 和 kNN 使用默认权重：

```text
global BM25 weight = 1.0
global kNN weight = 1.0
```

Branch 权重只有通过评测后才能提高。

### 8.4 Branch 候选和融合

建议初始预算：

```text
单次查询正式 Entity 分支上限：8
每个 Branch BM25 候选：24
每个 Branch kNN 候选：24
Branch 总候选池：100
全库 BM25 候选：24
全库 kNN 候选：24
全库候选池：48
最终返回候选：8 到 10
```

规则如下：

- 使用 `chunk_id` 作为 RRF 融合主键。
- 不直接比较 Branch 和全库的原始分数。
- 融合后按资源去重，单个资源默认最多保留 2 个 Chunk。
- Branch 候选必须执行 Phase 4 权限复核。
- Branch 权限失败不能降级为未过滤查询。

### 8.5 回退规则

以下情况直接退回传统 RAG：

- 未命中正式 Entity。
- Entity 解析存在歧义。
- Branch 结果为空。
- Branch 超时。
- Branch 权限过滤失败。
- Entity Registry 不可用或版本不匹配。

诊断至少记录：

```text
tree_mode
effective_execution_path
resolved_entity_count
resolved_branch_count
branch_candidate_count
branch_result_count
global_result_count
fused_result_count
fallback_reason
branch_duration_ms
```

诊断不得包含 protected 正文。

### 8.6 超时和熔断

- Branch 和全库并行执行。
- Branch 必须服从整体查询预算，不能让全库等待。
- Branch 超时只返回全库结果。
- Branch 错误不能使传统 RAG 请求失败。
- Shadow 不参与最终结果，也不允许拖慢正常响应。
- 出现严重质量回退或延迟异常时，运维人员将全局配置切回 `shadow` 或 `off`。
- MVP 不实现自动按组织熔断，因为配置不区分组织。

### 8.7 影子评测

同一批评测查询分别执行：

```text
traditional_rag
tree_shadow
tree_boost
```

至少记录：

```text
Recall@5
Recall@10
MRR
nDCG@10
branch_hit_rate
wrong_branch_rate
branch_candidate_count
branch_result_count
global_result_count
fused_result_count
candidate_reduction_ratio
fallback_reason
p50/p95 latency
权限泄露数
```

核心验收：

- `tree_boost` 的 Recall@k 不低于传统 RAG。
- Branch 选错时全库仍能返回正确 Chunk。
- 未命中 Entity 时不影响传统 RAG。
- Wrong Branch Rate 可量化。
- 权限泄露为零。
- Branch 带来的候选缩减必须同时报告 Recall 损失。
- 评测结果可以按组织和查询类型分析，但 Tree Mode 配置仍是全局。

### 8.8 完成标准

- Tree Mode 只通过全局配置控制。
- 默认为 `shadow`。
- `off` 可以完全恢复传统 RAG。
- `boost` 不排除全库结果。
- 树错误不会造成最终召回下降。
- 全库检索始终作为兜底通道。
- 可以量化分支命中率、错误分支率和候选缩减比例。
- 权限和租户隔离测试全部通过。
- 只有在测试证明无漏召后，才允许讨论排他过滤。

### 8.9 排他过滤

`filter` 不属于 Phase 6，也不属于 MVP。

未来只有满足以下条件后才允许讨论：

1. 评测集上 Branch 准确率稳定。
2. 高置信 Entity 的漏召回可以量化且可接受。
3. Shadow 对比证明 Branch 剪枝没有丢掉 gold Chunk。
4. 权限和租户隔离测试全部通过。
5. 有自动回滚和熔断机制。

即使未来引入 `filter`，也不能成为全局默认。

## 9. Phase 7：重建、切换和旧链路清理

### 9.1 目标

完成从旧结构到新结构的切换，并删除旧兼容路径。

### 9.2 步骤

1. 从 Knowledge 回放构建新结构。
2. 运行新旧检索影子对比。
3. 切换读路径。
4. 停止旧表写入。
5. 删除旧树、旧 Fact 和旧兼容字段。
6. API 只在接口层保留必要兼容。

### 9.3 需要讨论

- 回放范围。
- 新旧结果对比方式。
- 回滚时间窗口。
- 旧表保留时间。
- 是否允许双读，不允许双写。
- 失败任务恢复和重建重试。

### 9.4 完成标准

- 唯一的写入链路是新链路。
- 旧表不再被业务逻辑读取。
- 旧兼容字段和旧分支已有删除计划。
- 服务三可以完全从 Knowledge 重建。

## 10. Phase 8：Fact、Entity Tree 和 KV

### 10.1 状态

本阶段明确属于 MVP 之后，不阻塞前七个阶段。

### 10.2 后续范围

- Fact 抽取、版本和冲突。
- Entity 自动归并。
- Entity Tree 摘要和语义导航。
- KV key registry。
- KV 精确查询。
- Agent 工具选择。
- KV 权限。

### 10.3 原则

- Fact 不能成为 Chunk 入树的前置条件。
- KV 不能绕过 RAG 权限。
- Entity Tree 不能替代 Chunk 证据。
- Memory 失败不能影响基础 RAG 可用性。

## 11. MVP 固定范围

服务三 MVP 固定为：

```text
一个 KnowledgeItem 一个资源一个任务一个 rag_status
单 RAG Worker 多 Lane
Dispatcher + Parse + Index + Memory + Callback
传统 RAG 完整可用
metadata_only 完整处理
Core 权限接入
确定性 branch_keys
受控 Entity
树 off / shadow / boost
全局 RAG 始终兜底
旧链路停止扩展，不做双写
不做 Fact 依赖
不做 KV
不做排他树过滤
```

## 12. 仍需重点讨论的阶段

按优先级：

1. Phase 1：数据、事件和任务契约。
2. Phase 4：权限接入。
3. Phase 3：传统 RAG 基线和评测。
4. Phase 5：Domain、Entity 和候选准入。
5. Phase 6：树影子评测和启用条件。
6. Phase 7：数据迁移和旧链路删除。

Phase 0 和 Phase 2 的大方向已经基本明确，不需要反复讨论。

Phase 8 明确后置，避免 Fact 和 KV 再次进入基础 RAG 主链路。

## 13. 阶段推进规则

1. 每个阶段必须先冻结契约，再开始代码实现。
2. 契约变更必须明确版本、影响范围和数据重建方案。
3. 未达到当前阶段完成标准，不提前进入下一阶段。
4. 任何新字段必须说明属于对外状态、内部调度还是诊断信息。
5. 任何兼容逻辑只能存在于接口适配层，不能进入核心存储结构。
6. 每个阶段结束后更新对应设计文档和测试清单。
