# RAG 服务 MVP 数据库表结构设计

> 状态：MVP 设计基线
>
> 范围：`services/rag` 自有 PostgreSQL `rag` Schema
>
> 原则：RAG 表可以完全重构，不兼容旧 `memory_*` 字段；原始消息、附件、权限事实仍由 Knowledge/Core 持有。

## 部署说明

目标 Schema 仍为 `rag`。

为避免影响当前仍在运行的旧 RAG 连接，第一阶段将新表创建在 `rag_mvp` Schema：

```text
当前运行：
RAG_DATABASE_SCHEMA=rag

新结构预部署：
rag_mvp

切换时：
RAG_DATABASE_SCHEMA=rag_mvp
```

旧 `rag` 表、数据、索引和在线服务保持不变。新链路验证完成后，再决定把 `rag_mvp` 迁移为正式 `rag`，还是继续使用 `rag_mvp`。

## 1. 通用约定

### 1.1 主键和时间

- 主键统一使用 `UUID`，默认值使用 `gen_random_uuid()`。
- 时间统一使用 `TIMESTAMPTZ`。
- 创建时间默认 `CURRENT_TIMESTAMP`。
- 更新时间默认 `CURRENT_TIMESTAMP`，由业务写入时更新。
- 外键在文档中单独说明；RAG 不建立跨 `knowledge` Schema 的数据库外键。

PostgreSQL 没有 MySQL 的 `DATETIME` 类型。这里使用 `TIMESTAMPTZ` 保存绝对时间点，内部按 UTC 存储，输入和展示时转换为目标时区。

纯日期字段使用 `DATE`，不把业务日期伪装成某个时区的 `TIMESTAMPTZ`。需要固定当地执行时刻时，使用 `TIME` 加单独时区字段。

### 1.2 Scope

所有用户可见内容统一使用 Scope：

```text
scope_type: organization | user
scope_id: organization_id 或 owner_user_id
scope_key: organization:{scope_id} 或 user:{scope_id}
```

`scope_key` 使用 PostgreSQL 生成列：

```sql
GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED
```

MVP 是单组织模型：一个用户最多属于一个组织。

- 用户有组织时，可以搜索自己的私人 Scope 或当前唯一组织 Scope。
- 用户没有组织时，只能搜索自己的私人 Scope。
- 客户端只能表达 `private` 或 `organization` 搜索意图。
- 客户端不能提交权威的 `organization_id`、`owner_user_id` 或 `scope_key`。
- 服务端根据认证身份和用户唯一组织关系生成最终 `scope_type + scope_id`。

`scope_key` 是服务端生成的内部字段，不是客户端可自由指定的权威字段。对外请求只接受 `user_id`、`organization_id` 等身份字段，服务端完成认证和资源归属解析后再生成 `scope_key`。

### 1.3 索引标记

字段表中的“索引”列说明如下：

- `唯一 B-tree`：字段或字段组合有唯一约束。
- `B-tree`：普通 B-tree 索引。
- `复合 B-tree`：参与多字段索引或唯一约束。
- `部分 B-tree`：带 `WHERE` 条件的索引。
- `GIN`：数组或 JSONB 索引。
- `无`：当前设计不单独建立索引。

### 1.4 表清单

| 表名 | 用途 |
|---|---|
| `rag.processing_jobs` | RAG 资源处理任务和调度状态 |
| `rag.processing_job_attempts` | 每个 Lane/阶段的执行和重试记录 |
| `rag.outbox_events` | RAG 回写 Knowledge 的可靠事件 |
| `rag.resource_snapshots` | KnowledgeItem 的版本化资源快照 |
| `rag.chunks` | 实际可检索的 Chunk 变体 |
| `rag.entity_registry` | MVP 正式 Entity Registry |
| `rag.entity_aliases` | MVP 正式 Entity Alias |
| `rag.entity_candidates` | RAG 生成、供审核工作流使用的候选实体聚合记录 |
| `rag.entity_candidate_mentions` | 候选实体在 Chunk 中的证据 |
| `rag.entity_review_requests` | 候选实体审核请求的幂等和审计记录 |
| `rag.tree_nodes` | Scope、Domain、Entity、Time 导航目录 |
| `rag.chunk_branches` | Chunk 与 Branch Key、Entity 的关系 |
| `rag.branch_refresh_jobs` | Registry 变化后的 Branch Key 增量刷新任务 |
| `rag.projection_records` | PostgreSQL 到 Elasticsearch 的投影状态 |
| `rag.search_history` | 搜索请求历史和诊断 |
| `rag.qa_conversations` | AI 问答会话 |
| `rag.qa_messages` | AI 问答消息和引用 |

### 1.5 状态分层

业务状态与内部调度状态必须分开。

Knowledge 对外状态：

```text
pending
processing
ready
metadata_only
failed
cancelled
```

其中 `knowledge_items.rag_status` 是前端唯一业务状态来源。

RAG 内部任务状态：

```text
pending
leased
running
retry_wait
succeeded
failed
dead
cancelled
```

`processing_jobs.status` 只表示内部调度状态，`processing_jobs.current_stage` 只表示内部阶段。前端不得直接使用这两个字段拼接业务状态。

## 2. `rag.processing_jobs`

### 2.1 用途

保存一个 KnowledgeItem、一个可处理资源的 RAG 任务状态、租约、重试、版本和调度信息。普通处理任务和显式 `reindex` 任务都写入本表。

### 2.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 任务主键 | 是 | 唯一 B-tree |
| `source_event_id` | `UUID` | 无 | Knowledge 投递的事件 ID | 是 | 唯一 B-tree |
| `payload_hash` | `CHAR(64)` | 无 | 事件 payload 的 SHA-256，用于阻止同事件不同内容 | 无 | 无 |
| `job_type` | `VARCHAR(32)` | `'full_process'` | `full_process` 或 `reindex` | 是 | B-tree |
| `knowledge_item_id` | `UUID` | 无 | KnowledgeItem ID | 是 | 复合 B-tree |
| `resource_type` | `VARCHAR(16)` | 无 | `message` 或 `attachment` | 是 | 复合 B-tree |
| `resource_id` | `UUID` | 无 | 消息 ID 或附件 ID | 是 | 复合 B-tree |
| `knowledge_base_id` | `UUID` | 无 | 知识库 ID | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | `organization` 或 `user` | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | 组织 ID 或私人所有者 ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | `organization:{id}` 或 `user:{id}` | 是 | 复合 B-tree |
| `source_conversation_id` | `UUID` | 无 | Knowledge 内部来源会话 UUID | 是 | B-tree |
| `source_audience_policy` | `VARCHAR(32)` | 无 | 来源受众策略 | 无 | 无 |
| `content_version` | `INTEGER` | 无 | Knowledge 内容版本 | 是 | 复合 B-tree |
| `processing_version` | `VARCHAR(128)` | 无 | RAG 处理规则版本 | 是 | 复合 B-tree |
| `acl_version` | `BIGINT` | `0` | Knowledge/Core 权限版本 | 无 | 无 |
| `status` | `VARCHAR(32)` | `'pending'` | 内部调度状态：`pending/leased/running/retry_wait/succeeded/failed/dead/cancelled` | 是 | 部分 B-tree |
| `current_stage` | `VARCHAR(32)` | 无 | 当前阶段，例如 `fetch/parse/chunk/embed/index` | 是 | B-tree |
| `lease_owner` | `VARCHAR(128)` | 无 | 当前任务租约持有者 | 无 | 无 |
| `lease_until` | `TIMESTAMPTZ` | 无 | 租约过期时间 | 是 | 部分 B-tree |
| `retry_count` | `INTEGER` | `0` | 当前阶段累计重试次数 | 无 | 无 |
| `next_retry_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 下次允许重试时间 | 是 | 部分 B-tree |
| `last_error` | `TEXT` | 无 | 最近一次错误摘要 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 是 | B-tree |
| `started_at` | `TIMESTAMPTZ` | 无 | 开始处理时间 | 无 | 无 |
| `finished_at` | `TIMESTAMPTZ` | 无 | 完成时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 2.3 表级约束和索引

- 唯一约束：`source_event_id`。
- 部分唯一约束：`knowledge_item_id + resource_type + resource_id + content_version`，仅作用于 `job_type='full_process' AND status IN ('pending','leased','running','retry_wait')`。
- 部分唯一约束：`knowledge_item_id + resource_type + resource_id + content_version + processing_version`，仅作用于 `job_type='reindex' AND status IN ('pending','leased','running','retry_wait')`。
- 调度索引：`status + next_retry_at + lease_until`。

对外业务状态由 Knowledge 的 `rag_status` 持有并由 Callback Lane 更新，本表不保存 `ready`、`metadata_only` 等业务枚举。

## 3. `rag.processing_job_attempts`

### 3.1 用途

记录每次 Lane 和阶段的执行结果，作为重试、审计、耗时和错误排查的追加型记录。

### 3.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | Attempt 主键 | 是 | 唯一 B-tree |
| `job_id` | `UUID` | 无 | 对应的 `processing_jobs.id` | 是 | 复合 B-tree |
| `lane` | `VARCHAR(32)` | 无 | `parse/index/memory/callback` | 是 | B-tree |
| `stage` | `VARCHAR(32)` | 无 | 当前执行阶段 | 是 | 复合 B-tree |
| `attempt` | `INTEGER` | `1` | 当前阶段第几次尝试 | 是 | 复合 B-tree |
| `status` | `VARCHAR(16)` | `'running'` | `running/succeeded/failed/cancelled` | 是 | B-tree |
| `started_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 开始时间 | 无 | 无 |
| `finished_at` | `TIMESTAMPTZ` | 无 | 结束时间 | 无 | 无 |
| `retryable` | `BOOLEAN` | `FALSE` | 该错误是否允许重试 | 无 | 无 |
| `error_code` | `VARCHAR(64)` | 无 | 稳定错误码 | 无 | 无 |
| `error_message` | `TEXT` | 无 | 错误摘要，不保存敏感正文 | 无 | 无 |
| `metrics` | `JSONB` | `'{}'::jsonb` | 耗时、数量、模型调用等指标 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |

### 3.3 表级约束和索引

- 唯一约束：`job_id + stage + attempt`。
- 查询索引：`job_id + created_at`、`status + created_at`。

## 4. `rag.outbox_events`

### 4.1 用途

保存 RAG 回写 Knowledge 的任务状态事件，由 Callback Lane 发送并重试。

### 4.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | Outbox 事件 ID | 是 | 唯一 B-tree |
| `job_id` | `UUID` | 无 | 关联任务 ID | 是 | B-tree |
| `aggregate_type` | `VARCHAR(32)` | `'knowledge_item'` | 聚合类型 | 是 | 复合 B-tree |
| `aggregate_id` | `UUID` | 无 | 聚合 ID | 是 | 复合 B-tree |
| `event_type` | `VARCHAR(64)` | 无 | RAG 回调事件类型 | 是 | 复合 B-tree |
| `event_version` | `BIGINT` | `1` | 聚合事件版本 | 是 | 复合 B-tree |
| `schema_version` | `INTEGER` | `1` | 事件 schema 版本 | 无 | 无 |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `trace_id` | `VARCHAR(128)` | 无 | 链路追踪 ID | 是 | B-tree |
| `payload` | `JSONB` | `'{}'::jsonb` | 回调载荷 | 无 | 无 |
| `status` | `VARCHAR(16)` | `'pending'` | `pending/publishing/published/failed` | 是 | 复合 B-tree |
| `retry_count` | `INTEGER` | `0` | 投递重试次数 | 无 | 无 |
| `available_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 下次可投递时间 | 是 | 部分 B-tree |
| `published_at` | `TIMESTAMPTZ` | 无 | 成功投递时间 | 无 | 无 |
| `last_error` | `TEXT` | 无 | 最近投递错误 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 是 | B-tree |

### 4.3 表级约束和索引

- 唯一约束：`aggregate_type + aggregate_id + event_type + event_version`。
- 投递索引：`status + available_at`。

## 5. `rag.resource_snapshots`

### 5.1 用途

表示一个 KnowledgeItem 在某个内容版本下的稳定资源快照，不复制完整原文。

本表保存稳定的逻辑资源引用和可选的内部对象存储定位信息。`resource_ref` 是跨存储可用的逻辑引用；`object_ref` 可以保存 MinIO 的 bucket/key 等内部定位，但不得保存预签名 URL、访问令牌或客户端下载链接。

MVP 将采集结果视为不可变快照，不处理外部平台的编辑、撤回或删除。相同资源即使重复采集，也按 `knowledge_item_id + content_version` 幂等处理，不创建重复快照。

### 5.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 快照主键 | 是 | 唯一 B-tree |
| `knowledge_item_id` | `UUID` | 无 | KnowledgeItem ID | 是 | 复合 B-tree |
| `resource_type` | `VARCHAR(16)` | 无 | `message` 或 `attachment` | 是 | 复合 B-tree |
| `resource_id` | `UUID` | 无 | 消息 ID 或附件 ID | 是 | B-tree |
| `resource_ref` | `TEXT` | 无 | 稳定的逻辑资源引用 | 是 | B-tree |
| `knowledge_base_id` | `UUID` | 无 | 知识库 ID | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `source_conversation_id` | `UUID` | 无 | 来源会话内部 ID | 是 | B-tree |
| `source_conversation_type` | `VARCHAR(16)` | 无 | `group/private` | 无 | 无 |
| `source_audience_policy` | `VARCHAR(32)` | 无 | 来源受众策略 | 无 | 无 |
| `external_conversation_id` | `VARCHAR(255)` | 无 | 平台外部会话 ID | 无 | 无 |
| `storage_backend_id` | `VARCHAR(36)` | 无 | 内部存储后端 ID | 无 | 无 |
| `object_ref` | `TEXT` | 无 | MinIO bucket/key 等内部对象定位，不保存签名 URL | 无 | 无 |
| `sender_identity_id` | `UUID` | 无 | Knowledge 外部身份记录 ID，可为空 | 是 | B-tree |
| `sender_platform` | `VARCHAR(32)` | 无 | `feishu/wecom/wechat` | 是 | 复合 B-tree |
| `sender_workspace_key` | `VARCHAR(255)` | `''` | 发送人所在平台工作区 | 是 | 复合 B-tree |
| `sender_external_user_id` | `VARCHAR(255)` | 无 | 平台外部用户 ID，可为空 | 是 | 复合 B-tree |
| `sender_mapped_user_id` | `UUID` | 无 | 映射后的内部用户 ID，可为空 | 是 | B-tree |
| `sender_display_name` | `VARCHAR(255)` | 无 | 采集时发送人展示名 | 无 | 无 |
| `content_version` | `INTEGER` | `1` | Knowledge 内容版本 | 是 | 复合 B-tree |
| `content_hash` | `CHAR(64)` | 无 | 权威内容 SHA-256 | 是 | B-tree |
| `acl_version` | `BIGINT` | `0` | 权限版本 | 无 | 无 |
| `access_scope` | `VARCHAR(32)` | 无 | 访问范围 | 无 | 无 |
| `sensitivity` | `VARCHAR(32)` | 无 | 敏感等级 | 无 | 无 |
| `has_display_content` | `BOOLEAN` | `FALSE` | 是否存在 display 变体 | 无 | 无 |
| `has_protected_content` | `BOOLEAN` | `FALSE` | 是否存在 protected 变体 | 无 | 无 |
| `lifecycle_status` | `VARCHAR(16)` | `'active'` | `active/deleted/inactive` | 是 | 复合 B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 5.3 表级约束和索引

- 唯一约束：`knowledge_item_id + content_version`。
- 查询索引：`scope_type + scope_id + knowledge_base_id + lifecycle_status`。
- 来源索引：`source_conversation_id + content_version`。
- 发送人身份索引：`sender_identity_id`。
- 外部身份索引：`sender_platform + sender_workspace_key + sender_external_user_id`。
- 内部用户索引：`sender_mapped_user_id`。

## 6. `rag.chunks`

### 6.1 用途

保存可检索 Chunk 变体正文和来源定位。display 与 protected 使用不同 `content_variant` 行。

MVP 假设采集后的外部内容不可变，不处理外部修改、撤回或删除，因此暂不单独保存 `logical_chunk_id`。`chunk_id` 是当前来源版本和处理版本下的确定性物理 Chunk ID：

```text
sha256(
  knowledge_item_id
  + resource_type
  + resource_id
  + content_version
  + processing_version
  + chunking_version
  + content_variant
  + chunk_index
)
```

如果后续需要外部版本共存、跨版本引用或 Chunk 编辑历史，再引入单独的 `logical_chunk_id` 并执行统一回填。

display 和 protected 变体必须在查询阶段通过逻辑位置去重：

```text
knowledge_item_id
+ content_version
+ processing_version
+ chunking_version
+ chunk_index
```

查询侧根据上述字段计算临时 `logical_chunk_key`，不写入 PostgreSQL。

去重规则：

- 用户有权访问 protected 时，保留 protected，丢弃 display。
- 用户无权访问 protected 时，只保留 display。
- 去重应在 RRF 前完成，避免同一逻辑 Chunk 从两个索引获得重复票数。
- 最终引用中同一逻辑位置只能出现一次。

### 6.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 行主键 | 是 | 唯一 B-tree |
| `chunk_id` | `CHAR(64)` | 无 | 确定性 Chunk 检索键 | 是 | 唯一 B-tree |
| `resource_snapshot_id` | `UUID` | 无 | 所属资源快照 | 是 | 复合 B-tree |
| `knowledge_item_id` | `UUID` | 无 | KnowledgeItem ID | 是 | 复合 B-tree |
| `resource_type` | `VARCHAR(16)` | 无 | `message` 或 `attachment` | 无 | 无 |
| `resource_id` | `UUID` | 无 | 消息或附件 ID | 无 | 无 |
| `knowledge_base_id` | `UUID` | 无 | 知识库 ID | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `source_conversation_id` | `UUID` | 无 | 内部来源会话 ID | 是 | 复合 B-tree |
| `conversation_type` | `VARCHAR(16)` | 无 | `group/private` | 无 | 无 |
| `document_id` | `UUID` | 无 | 文档资源 ID | 是 | B-tree |
| `message_id` | `UUID` | 无 | 消息 ID | 是 | B-tree |
| `content_version` | `INTEGER` | 无 | 内容版本 | 是 | 复合 B-tree |
| `processing_version` | `VARCHAR(128)` | 无 | RAG 处理规则版本 | 是 | 复合 B-tree |
| `chunking_version` | `VARCHAR(64)` | 无 | 切块规则版本 | 是 | 复合 B-tree |
| `content_variant` | `VARCHAR(16)` | 无 | `display` 或 `protected` | 是 | 复合 B-tree |
| `chunk_index` | `INTEGER` | 无 | Chunk 顺序 | 是 | 复合 B-tree |
| `chunk_count` | `INTEGER` | 无 | 当前资源 Chunk 总数 | 无 | 无 |
| `title` | `VARCHAR(512)` | 无 | 标题 | 无 | 无 |
| `file_name` | `VARCHAR(512)` | 无 | 文件名 | 无 | 无 |
| `heading_path` | `TEXT[]` | `'{}'` | 章节路径数组 | 是 | GIN |
| `context_header` | `JSONB` | `'{}'::jsonb` | 标题、来源等结构化上下文 | 无 | 无 |
| `content` | `TEXT` | 无 | Chunk 正文 | 无 | 无 |
| `content_hash` | `CHAR(64)` | 无 | Chunk 正文 SHA-256 | 是 | B-tree |
| `source_locator` | `JSONB` | `'{}'::jsonb` | 页码、段落、表格定位 | 无 | 无 |
| `sent_at` | `TIMESTAMPTZ` | 无 | 来源发送时间 | 是 | 复合 B-tree |
| `auth_partition_key` | `TEXT` | 无 | 权限分区键 | 是 | B-tree |
| `auth_object_key` | `TEXT` | 无 | protected 精确授权对象键 | 是 | B-tree |
| `acl_version` | `BIGINT` | `0` | 权限版本 | 无 | 无 |
| `sensitivity` | `VARCHAR(32)` | 无 | 敏感等级 | 无 | 无 |
| `embedding_model` | `VARCHAR(128)` | 无 | Embedding 模型 | 无 | 无 |
| `embedding_dimensions` | `INTEGER` | 无 | 向量维度 | 无 | 无 |
| `embedding_status` | `VARCHAR(16)` | `'pending'` | `pending/ready/failed` | 是 | 复合 B-tree |
| `rag_eligible` | `BOOLEAN` | `TRUE` | 是否允许进入 AI/RAG 检索 | 无 | 无 |
| `lifecycle_status` | `VARCHAR(16)` | `'active'` | 生命周期状态 | 是 | 复合 B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 6.3 表级约束和索引

- 唯一约束：`chunk_id`。
- 唯一约束：`knowledge_item_id + content_version + processing_version + content_variant + chunk_index`。
- 顺序索引：`resource_snapshot_id + chunk_index`。
- Scope 索引：`scope_type + scope_id + knowledge_base_id + lifecycle_status`。
- 会话索引：`source_conversation_id + sent_at`。
- 投影索引：`content_variant + embedding_status`。

## 7. `rag.entity_registry`

### 7.1 用途

保存正式 Entity。只有经过受控来源确认的实体才能进入本表。

归属说明：MVP 由 RAG 持有并维护 `entity_registry`、`entity_aliases` 和候选实体。Core 只管理系统用户、组织、租户和授权主体。RAG 的 Entity Registry 必须支持版本化导出、导入和备份。

Entity 的来源包括人工维护、受控配置导入、稳定标识和候选审核晋升。外部平台用户 ID、联系人 ID、消息 ID 和附件 ID 不能直接作为 `entity_id`。

后续如果 Knowledge/Core 需要统一管理业务实体目录，再将 Entity Registry 权威迁移到对应领域服务，RAG 转为版本化读取投影。

### 7.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | Entity 主键，创建后不变 | 是 | 唯一 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `domain` | `VARCHAR(32)` | 无 | 固定 Domain | 是 | 复合 B-tree |
| `canonical_name` | `VARCHAR(512)` | 无 | 规范名称 | 无 | 无 |
| `normalized_key` | `VARCHAR(512)` | 无 | 归一化名称 | 是 | 唯一 B-tree |
| `status` | `VARCHAR(16)` | `'active'` | `active/disabled/merged` | 是 | 复合 B-tree |
| `merged_into_entity_id` | `UUID` | 无 | 合并后的主 Entity | 是 | B-tree |
| `registry_version` | `BIGINT` | `1` | Registry 版本 | 是 | B-tree |
| `created_by` | `UUID` | 无 | 创建人或管理员 ID | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 7.3 表级约束和索引

- 唯一约束：`scope_type + scope_id + domain + normalized_key`。
- 查询索引：`scope_type + scope_id + domain + status`。
- 自引用外键：`merged_into_entity_id -> entity_registry.id`。

## 8. `rag.entity_aliases`

### 8.1 用途

保存正式 Entity 的规范别名，保证同名别名不会映射到多个 Entity。

### 8.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 别名主键 | 是 | 唯一 B-tree |
| `entity_id` | `UUID` | 无 | 所属正式 Entity | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `domain` | `VARCHAR(32)` | 无 | Entity Domain | 是 | 复合 B-tree |
| `display_alias` | `VARCHAR(512)` | 无 | 展示别名 | 无 | 无 |
| `normalized_alias` | `VARCHAR(512)` | 无 | 归一化别名 | 是 | 唯一 B-tree |
| `status` | `VARCHAR(16)` | `'active'` | `active/disabled` | 是 | 复合 B-tree |
| `source` | `VARCHAR(32)` | 无 | `manual/directory/import/confirmed` | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 8.3 表级约束和索引

- 唯一约束：`scope_type + scope_id + domain + normalized_alias`。
- 外键：`entity_id -> entity_registry.id`。
- 查询索引：`entity_id + status`。

## 9. `rag.entity_candidates`

### 9.1 用途

保存从 display 或脱敏内容中发现的候选实体聚合。候选实体不进入正式树，也不参与正式检索。

### 9.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 候选主键 | 是 | 唯一 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `candidate_name` | `VARCHAR(512)` | 无 | 候选展示名称 | 无 | 无 |
| `normalized_key` | `VARCHAR(512)` | 无 | 归一化候选名称 | 是 | 唯一 B-tree |
| `candidate_domain` | `VARCHAR(32)` | `'unclassified'` | 候选 Domain | 是 | 复合 B-tree |
| `mention_count` | `INTEGER` | `0` | 出现次数 | 无 | 无 |
| `distinct_chunk_count` | `INTEGER` | `0` | 不同 Chunk 数 | 无 | 无 |
| `distinct_source_count` | `INTEGER` | `0` | 不同资源数 | 无 | 无 |
| `distinct_conversation_count` | `INTEGER` | `0` | 不同会话数 | 无 | 无 |
| `first_seen_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 首次出现时间 | 无 | 无 |
| `last_seen_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 最近出现时间 | 是 | B-tree |
| `sample_context` | `TEXT` | 无 | 脱敏样例上下文 | 无 | 无 |
| `suggested_entity_id` | `UUID` | 无 | 建议合并的正式 Entity | 是 | B-tree |
| `score` | `NUMERIC(5,4)` | `0` | 确定性候选评分 | 是 | 复合 B-tree |
| `status` | `VARCHAR(16)` | `'new'` | `new/grouped/review_ready/merged/promoted/ignored/deferred` | 是 | 复合 B-tree |
| `resolved_entity_id` | `UUID` | 无 | 审核后合并或创建得到的正式 Entity | 是 | B-tree |
| `reviewed_by` | `UUID` | 无 | 审核人 ID | 是 | 复合 B-tree |
| `reviewed_at` | `TIMESTAMPTZ` | 无 | 审核时间 | 是 | 复合 B-tree |
| `review_note` | `TEXT` | 无 | 审核备注 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 9.3 表级约束和索引

- 唯一约束：`scope_type + scope_id + candidate_domain + normalized_key`。
- 审核索引：`scope_type + scope_id + status + score DESC`。
- 建议合并索引：`suggested_entity_id`。
- 审核审计索引：`reviewed_by + reviewed_at DESC`。

## 10. `rag.entity_candidate_mentions`

### 10.1 用途

保存候选实体在 Chunk 中的每次出现，作为评分、解释和审核证据。上下文必须是脱敏内容。

### 10.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | Mention 主键 | 是 | 唯一 B-tree |
| `candidate_id` | `UUID` | 无 | 所属候选实体 | 是 | 复合 B-tree |
| `chunk_id` | `CHAR(64)` | 无 | 来源 Chunk | 是 | B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `surface_form` | `VARCHAR(512)` | 无 | 文本中实际出现的名称 | 无 | 无 |
| `candidate_domain` | `VARCHAR(32)` | `'unclassified'` | 本次 mention 的候选 Domain | 无 | 无 |
| `context_excerpt` | `TEXT` | 无 | 脱敏上下文片段 | 无 | 无 |
| `confidence` | `NUMERIC(5,4)` | `0` | 规则或 LLM 置信度 | 无 | 无 |
| `extraction_method` | `VARCHAR(32)` | 无 | `regex/metadata/llm` | 是 | B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 是 | 复合 B-tree |

### 10.3 表级约束和索引

- 外键：`candidate_id -> entity_candidates.id`。
- 普通外键：`chunk_id -> chunks.chunk_id`。
- 查询索引：`candidate_id + created_at`。
- 去重索引：`candidate_id + chunk_id + surface_form + extraction_method`。

## 10A. `rag.entity_review_requests`

### 10A.1 用途

保存每次候选实体审核请求，提供重试幂等、并发保护和审计。

### 10A.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 审核请求主键 | 是 | 唯一 B-tree |
| `candidate_id` | `UUID` | 无 | 对应候选实体 | 是 | 复合唯一 B-tree |
| `review_request_id` | `UUID` | 无 | 客户端审核请求 ID | 是 | 复合唯一 B-tree |
| `action` | `VARCHAR(16)` | 无 | `promote/merge/ignore/defer` | 无 | 无 |
| `target_entity_id` | `UUID` | 无 | merge 目标 Entity | 是 | B-tree |
| `reviewer_id` | `UUID` | 无 | 审核人 | 是 | B-tree |
| `expected_status` | `VARCHAR(16)` | 无 | 审核前期望状态 | 无 | 无 |
| `result_status` | `VARCHAR(16)` | 无 | 审核后状态 | 无 | 无 |
| `registry_version` | `BIGINT` | 无 | 审核后的 Registry 版本 | 是 | B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 是 | B-tree |

### 10A.3 表级约束和索引

- 唯一约束：`candidate_id + review_request_id`。
- 外键：`candidate_id -> entity_candidates.id`。
- 外键：`target_entity_id -> entity_registry.id`。
- 审核人审计索引：`reviewer_id + created_at DESC`。

## 11. `rag.tree_nodes`

### 11.1 用途

保存 Scope、Domain、Entity、Time 的导航目录。节点只保存导航和统计，不保存 Chunk 正文、Chunk ID 数组或 Embedding。

### 11.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 节点主键 | 是 | 唯一 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `domain` | `VARCHAR(32)` | 无 | 节点 Domain | 是 | 复合 B-tree |
| `entity_id` | `UUID` | 无 | 对应正式 Entity | 是 | 复合 B-tree |
| `time_bucket` | `VARCHAR(7)` | 无 | `YYYY-MM` | 是 | 复合 B-tree |
| `parent_id` | `UUID` | 无 | 父节点 | 是 | B-tree |
| `node_type` | `VARCHAR(16)` | 无 | `scope/domain/entity/time` | 是 | B-tree |
| `node_key` | `VARCHAR(512)` | 无 | 节点稳定键 | 是 | 唯一 B-tree |
| `branch_key` | `VARCHAR(512)` | 无 | Chunk 使用的 Branch Key | 是 | 唯一 B-tree |
| `registry_version` | `BIGINT` | `1` | Entity Registry 版本 | 是 | B-tree |
| `status` | `VARCHAR(16)` | `'active'` | `active/archived` | 是 | 复合 B-tree |
| `statistics` | `JSONB` | `'{}'::jsonb` | 节点统计，不保存正文 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 11.3 表级约束和索引

- 唯一约束：`scope_type + scope_id + branch_key`。
- 唯一约束：`scope_type + scope_id + node_key`。
- 树遍历索引：`parent_id + node_type`。
- Entity/时间索引：`entity_id + time_bucket`。
- 自引用外键：`parent_id -> tree_nodes.id`。

## 12. `rag.chunk_branches`

### 12.1 用途

保存 Chunk 与正式 Branch、Entity 的多对多关系。Elasticsearch 的 `branch_keys` 是这里聚合后的投影。

### 12.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `chunk_id` | `CHAR(64)` | 无 | 对应 Chunk | 是 | 复合唯一 B-tree |
| `branch_key` | `VARCHAR(512)` | 无 | Branch Key | 是 | 复合唯一 B-tree |
| `entity_id` | `UUID` | 无 | 正式 Entity | 是 | B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `registry_version` | `BIGINT` | 无 | 匹配时 Registry 版本 | 是 | B-tree |
| `match_method` | `VARCHAR(32)` | `'exact'` | `exact/alias/confirmed` | 无 | 无 |
| `match_score` | `NUMERIC(5,4)` | `1` | 匹配分数 | 无 | 无 |
| `status` | `VARCHAR(16)` | `'active'` | `active/removed` | 是 | 复合 B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 12.3 表级约束和索引

- 主键：`chunk_id + branch_key`。
- 普通外键：`chunk_id -> chunks.chunk_id`。
- 查询索引：`branch_key + status`。
- 刷新索引：`entity_id + registry_version + status`。

## 13. `rag.branch_refresh_jobs`

### 13.1 用途

Entity Registry 变化后，增量刷新受影响 Chunk 的 Branch Key。

### 13.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 刷新任务主键 | 是 | 唯一 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `entity_id` | `UUID` | 无 | 发生变化的 Entity | 是 | 复合 B-tree |
| `registry_version` | `BIGINT` | 无 | 目标 Registry 版本 | 是 | 复合 B-tree |
| `status` | `VARCHAR(16)` | `'pending'` | `pending/processing/succeeded/failed` | 是 | 复合 B-tree |
| `target_count` | `INTEGER` | `0` | 预计影响的 Chunk 数 | 无 | 无 |
| `processed_count` | `INTEGER` | `0` | 已处理 Chunk 数 | 无 | 无 |
| `retry_count` | `INTEGER` | `0` | 重试次数 | 无 | 无 |
| `next_retry_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 下次重试时间 | 是 | 部分 B-tree |
| `last_error` | `TEXT` | 无 | 最近错误 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `started_at` | `TIMESTAMPTZ` | 无 | 开始时间 | 无 | 无 |
| `finished_at` | `TIMESTAMPTZ` | 无 | 完成时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 13.3 表级约束和索引

- 部分唯一索引：`entity_id + registry_version`，仅作用于未终态任务。
- 调度索引：`status + next_retry_at`。

## 14. `rag.projection_records`

### 14.1 用途

保存 PostgreSQL Chunk 到 Elasticsearch 文档的投影状态，用于幂等写入、重试和重建。

### 14.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 投影记录主键 | 是 | 唯一 B-tree |
| `chunk_id` | `CHAR(64)` | 无 | 对应 Chunk | 是 | 复合 B-tree |
| `chunk_variant` | `VARCHAR(16)` | 无 | `display` 或 `protected` | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `knowledge_base_id` | `UUID` | 无 | 知识库 ID | 是 | 复合 B-tree |
| `es_index_alias` | `VARCHAR(255)` | 无 | 目标读别名 | 是 | 复合 B-tree |
| `es_document_id` | `VARCHAR(255)` | 无 | ES 文档 ID | 无 | 无 |
| `mapping_version` | `VARCHAR(32)` | 无 | ES mapping 版本 | 是 | 复合 B-tree |
| `embedding_model` | `VARCHAR(128)` | 无 | Embedding 模型 | 无 | 无 |
| `status` | `VARCHAR(16)` | `'pending'` | `pending/indexing/ready/failed/deleted` | 是 | 复合 B-tree |
| `indexed_at` | `TIMESTAMPTZ` | 无 | 成功索引时间 | 无 | 无 |
| `last_error` | `TEXT` | 无 | 最近投影错误 | 无 | 无 |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 无 | 无 |

### 14.3 表级约束和索引

- 唯一约束：`chunk_id + chunk_variant + mapping_version`。
- 重试索引：`status + es_index_alias`。
- Scope 索引：`scope_type + scope_id + knowledge_base_id`。

## 15. `rag.search_history`

### 15.1 用途

保存搜索请求、过滤条件、耗时和检索诊断。诊断不保存 protected 正文。

MVP 不保存原始查询。只保存查询哈希、脱敏查询和诊断信息，避免引入查询原文加密、密钥管理和额外审计存储。

### 15.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 搜索记录主键 | 是 | 唯一 B-tree |
| `user_id` | `UUID` | 无 | 发起搜索的用户 | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `query_hash` | `CHAR(64)` | 无 | 查询归一化 SHA-256 | 是 | B-tree |
| `query_redacted` | `TEXT` | 无 | 脱敏查询文本，可长期保留 | 无 | 无 |
| `filters` | `JSONB` | `'{}'::jsonb` | 请求过滤条件 | 无 | 无 |
| `tree_mode` | `VARCHAR(16)` | 无 | `off/shadow/boost` | 是 | 复合 B-tree |
| `execution_path` | `VARCHAR(32)` | 无 | 实际执行路径 | 无 | 无 |
| `diagnostics` | `JSONB` | `'{}'::jsonb` | 候选数、耗时、降级原因等 | 无 | 无 |
| `result_count` | `INTEGER` | `0` | 返回结果数 | 无 | 无 |
| `duration_ms` | `INTEGER` | 无 | 总耗时 | 无 | 无 |
| `request_id` | `VARCHAR(100)` | 无 | 请求 ID | 是 | B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 是 | 复合 B-tree |

### 15.3 表级约束和索引

- 用户历史索引：`user_id + created_at DESC`。
- Scope 历史索引：`scope_type + scope_id + created_at DESC`。
- 模式分析索引：`tree_mode + created_at DESC`。

## 16. `rag.qa_conversations`

### 16.1 用途

保存 AI 问答会话及会话级检索配置。

### 16.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 会话主键 | 是 | 唯一 B-tree |
| `user_id` | `UUID` | 无 | 所属用户 | 是 | 复合 B-tree |
| `scope_type` | `VARCHAR(16)` | 无 | 组织或用户 Scope | 是 | 复合 B-tree |
| `scope_id` | `UUID` | 无 | Scope ID | 是 | 复合 B-tree |
| `scope_key` | `TEXT` | 生成列 | Scope 文本键 | 是 | 复合 B-tree |
| `title` | `VARCHAR(300)` | 无 | 会话标题 | 无 | 无 |
| `status` | `VARCHAR(32)` | `'active'` | `active/archived/deleted` | 是 | B-tree |
| `retrieval_mode` | `VARCHAR(16)` | `'quick'` | `quick/deep` | 无 | 无 |
| `knowledge_base_ids` | `UUID[]` | `'{}'` | 会话绑定的知识库 | 是 | GIN |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 无 | 无 |
| `updated_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 更新时间 | 是 | 部分 B-tree |
| `deleted_at` | `TIMESTAMPTZ` | 无 | 软删除时间 | 是 | B-tree |

### 16.3 表级约束和索引

- 用户活动会话索引：`user_id + updated_at DESC`，条件为 `status='active' AND deleted_at IS NULL`。
- Scope 索引：`scope_type + scope_id + updated_at DESC`。

## 17. `rag.qa_messages`

### 17.1 用途

保存问答用户消息、助手回答、引用、模型信息和生成状态。

失败消息只保存安全、结构化的错误摘要。模型原始响应、完整系统 Prompt、上游请求体、Token 和用户敏感上下文不写入本表；完整诊断通过独立 trace 或受控诊断存储保留。

### 17.2 字段

| 字段 | 类型 | 默认值 | 含义 | 索引 | 索引类型 |
|---|---|---|---|---|---|
| `id` | `UUID` | `gen_random_uuid()` | 消息主键 | 是 | 唯一 B-tree |
| `conversation_id` | `UUID` | 无 | 所属问答会话 | 是 | 复合 B-tree |
| `parent_message_id` | `UUID` | 无 | 重新生成时的父消息 | 是 | B-tree |
| `role` | `VARCHAR(16)` | 无 | `user/assistant/system` | 无 | 无 |
| `content` | `TEXT` | 无 | 消息正文 | 无 | 无 |
| `citations` | `JSONB` | `'[]'::jsonb` | 引用数组 | 无 | 无 |
| `model_name` | `VARCHAR(128)` | 无 | 生成模型 | 是 | B-tree |
| `prompt_version` | `VARCHAR(64)` | 无 | Prompt 版本 | 是 | B-tree |
| `status` | `VARCHAR(32)` | `'completed'` | `streaming/completed/failed/cancelled` | 是 | B-tree |
| `token_usage` | `JSONB` | `'{}'::jsonb` | Token 统计 | 无 | 无 |
| `duration_ms` | `INTEGER` | 无 | 生成耗时 | 无 | 无 |
| `error_code` | `VARCHAR(64)` | 无 | 稳定错误码 | 是 | B-tree |
| `error_stage` | `VARCHAR(32)` | 无 | 失败阶段 | 无 | 无 |
| `error_class` | `VARCHAR(128)` | 无 | 错误类型，不保存异常堆栈 | 无 | 无 |
| `error_message_safe` | `TEXT` | 无 | 可安全展示的错误摘要 | 无 | 无 |
| `retryable` | `BOOLEAN` | `FALSE` | 是否可重试 | 无 | 无 |
| `diagnostic_ref` | `VARCHAR(128)` | 无 | 外部 trace 或受控诊断记录引用 | 是 | B-tree |
| `created_at` | `TIMESTAMPTZ` | `CURRENT_TIMESTAMP` | 创建时间 | 是 | 复合 B-tree |

### 17.3 表级约束和索引

- 外键：`conversation_id -> qa_conversations.id`。
- 自引用外键：`parent_message_id -> qa_messages.id`。
- 会话消息索引：`conversation_id + created_at`。
- Prompt 和模型排障索引：`model_name`、`prompt_version`、`status`。

## 18. 表关系

```text
processing_jobs
  -> processing_job_attempts
  -> outbox_events
  -> resource_snapshots
      -> chunks
          -> chunk_branches
          -> projection_records
          -> entity_candidate_mentions

entity_registry
  -> entity_aliases
  -> tree_nodes
  -> chunk_branches
  -> branch_refresh_jobs
  -> entity_candidates.suggested_entity_id
  -> entity_candidates.resolved_entity_id

entity_candidates
  -> entity_candidate_mentions
  -> entity_review_requests

qa_conversations
  -> qa_messages
```

## 19. 旧表处理

### 19.1 保留并重构

```text
processing_jobs
outbox_events
search_history
qa_conversations
qa_messages
```

### 19.2 替换

| 旧表 | 新结构 |
|---|---|
| `memory_sources` | `resource_snapshots` |
| `memory_chunks` | `chunks` |
| `memory_entities` | Knowledge 权威 Entity Registry/Alias + RAG 检索投影 |
| `memory_trees` | `tree_nodes` |
| `memory_nodes` | `tree_nodes` |
| `memory_node_sources` | `chunk_branches` 或 `resource_snapshots` |
| `index_records` | `projection_records` |

### 19.3 删除

```text
memory_facts
memory_fact_versions
memory_fact_chunks
memory_node_facts
```

旧 Fact 和旧 Node 结构不属于 MVP。Fact、版本和冲突处理推迟到 Phase 8，并使用新的表设计，不迁移旧表。

## 20. 迁移原则

1. 新表创建完成后，不修改旧表字段含义。
2. RAG 只写新结构，不进行新旧双写。
3. 从 Knowledge 回放生成 `resource_snapshots`、`chunks` 和 ES 文档。
4. RAG 通过导入、人工维护和候选确认建立并持有 Entity Registry/Alias，同时输出版本化导出用于备份和后续迁移。
5. 影子对比通过后切换读路径。
6. 旧表进入只读回滚窗口。
7. 回滚窗口结束后删除旧 `memory_*` 表和旧 ES 索引。

## 21. 已冻结决策

1. `resource_snapshots` 保存稳定的 `resource_ref`，可选保存内部 `storage_backend_id` 和 `object_ref`；不保存预签名 URL、访问令牌或客户端下载链接。
2. MVP 的外部采集内容不可变，`chunks` 不单独保存 `logical_chunk_id`。确定性 `chunk_id` 由 `knowledge_item_id`、`resource_type`、`resource_id`、内容版本、处理版本、切分版本、Content Variant 和顺序共同生成。display/protected 使用逻辑位置组合键在查询侧去重，有权时优先 protected。
3. MVP 由 RAG 持有并维护 `entity_registry`、`entity_aliases` 和候选实体。Core 只管理用户、组织、租户和授权主体。Entity Registry 支持版本化导出、导入和备份；后续如果需要迁移到 Knowledge/Core，再建立领域权威。
4. MVP 不保存原始 `query_text`。`search_history` 只长期保存 `query_hash`、`query_redacted` 和检索诊断，不引入查询原文加密和密钥管理链路。
5. `qa_messages` 不保存失败回答的完整原始错误、堆栈、Prompt 或上游响应。只保存稳定错误码、安全摘要、失败阶段、重试标记和诊断引用。
6. 业务状态与内部调度状态分开。`knowledge_items.rag_status` 是前端唯一业务状态，`processing_jobs.status` 和 `current_stage` 只用于 RAG 内部调度与排障。
7. `scope_key` 是服务端生成的内部字段。MVP 是单组织模型，客户端只表达私人或组织搜索意图，服务端根据认证身份和用户唯一组织关系生成最终 Scope；不接受客户端提交的权威 `organization_id`、`owner_user_id` 或 `scope_key`。

## 22. 候选实体审核与权限

候选实体不进入正式树，不参与正式检索或排他过滤。前端在独立的“候选实体/待审核”页面展示，不把待审核节点混入正式 Entity Tree。

### 22.1 前端展示

审核列表按 `entity_candidates` 聚合，不逐条展示原始消息。每条候选至少展示：

```text
候选名称
建议 Domain
出现次数
不同 Chunk、资源、会话数量
首次和最近出现时间
确定性评分
建议合并的正式 Entity
当前审核状态
```

详情页展示候选对应的脱敏证据：

```text
来源 Chunk
原文片段
抽取方法
置信度
已有实体匹配情况
冲突实体
```

审核动作对应状态流转：

```text
合并到已有 Entity -> merged
创建新 Entity     -> promoted
忽略              -> ignored
暂缓              -> deferred
```

审核完成后：

- 更新 `entity_candidates.status`、`resolved_entity_id`、`reviewed_by`、`reviewed_at` 和 `review_note`。
- 人工决策直接持久化到 RAG 的 Entity Registry/Alias。
- RAG 更新正式 Entity 和 Alias，并增加 `registry_version`。
- RAG 根据新的 Registry 版本刷新 `tree_nodes`、`chunk_branches` 和 ES Chunk 投影。
- 被忽略的候选不创建 Entity，不影响传统 RAG 检索。

### 22.2 权限

审核权限使用能力而非只绑定角色名：

```text
entity:read
entity:review
entity:merge
entity:admin
```

默认权限：

| 身份 | 权限 |
|---|---|
| Owner/Admin | 可以审核、创建、合并、禁用和归档正式 Entity |
| 指定信息管理员 | 可以在授权 Scope 或知识库范围内审核 |
| Contributor | 只能提出候选或提交审核建议 |
| Viewer | 只能查看正式 Entity，不能审核 |
| 私人空间所有者 | 只能审核自己的私人候选 |

组织共享知识库的审核权限属于源空间或组织管理员。被共享空间只能按授权读取正式结果，不能修改源空间 Entity。每次审核必须记录 Scope、审核人、时间和审计结果。
