# RAG 服务 MVP 存储契约收口

> 状态：已确认设计基线
>
> 配套文档：
>
> - `RAG服务MVP数据库表结构设计.md`
> - `RAG服务MVP ES索引设计.md`

## 1. 目标

本文冻结 PostgreSQL、Elasticsearch 和 Knowledge 回源之间的字段归属，解决以下问题：

- 每个检索字段的权威来源是什么。
- 哪些字段由 PostgreSQL 保存。
- 哪些字段只在 Elasticsearch 中存在。
- Elasticsearch 如何从 PostgreSQL 重建。
- Knowledge 字段如何写入 RAG。
- 同一字段变化时如何避免旧数据覆盖新数据。

本文确认后，再回写 PostgreSQL 和 Elasticsearch 两份基础设计稿。

## 2. 存储职责

### 2.1 Knowledge

Knowledge 是原始业务数据的权威来源：

```text
KnowledgeItem
消息
附件元数据
来源会话
发送人
内容版本
ACL 版本
原始内容对象引用
来源受众策略
```

RAG 只能通过事件和内部 HTTP 回源读取，不能直接访问 Knowledge 数据库。

### 2.2 RAG PostgreSQL

RAG PostgreSQL 是派生数据的权威来源：

```text
处理任务
任务执行记录
资源快照
Chunk 正文
Entity Registry
Entity Alias
Entity Candidate
Tree Node
Chunk Branch
投影状态
搜索历史
QA 会话
```

### 2.3 Elasticsearch

Elasticsearch 只保存可重建的 Chunk 检索投影和向量：

```text
BM25 检索字段
kNN 向量
Branch Key
权限过滤字段
来源过滤字段
展示字段
```

Elasticsearch 不是业务权威来源。

## 3. 向量存储决策

### 3.1 已确认决策

向量只保存在 Elasticsearch，不保存到 PostgreSQL。

PostgreSQL `chunks` 只保存：

```text
embedding_model
embedding_dimensions
embedding_status
```

PostgreSQL 不保存：

```text
embedding 向量数组
向量二进制
向量附件
```

### 3.2 重建规则

Elasticsearch 丢失或 Mapping 升级时：

```text
读取 PostgreSQL Chunk 正文
-> 调用 Embedding 服务重新生成向量
-> 批量写入新 ES 索引
-> 校验文档数量和向量维度
-> 切换读别名
-> 删除旧 ES 索引
```

约束：

- 重建必须校验 `embedding_model` 和 `embedding_dimensions`。
- Embedding 失败时重建任务失败并重试。
- 不允许跳过向量后把 Chunk 标记为 `ready`。
- 模型升级时产生新的 `processing_version`。
- 同一个物理索引只能使用一个模型和一组维度。
- 旧 ES 索引删除后，旧向量不保留。

## 4. 发送人字段

### 4.1 问题

Elasticsearch 需要：

```text
sender_identity_id
sender_display_name
```

当前 PostgreSQL `chunks` 设计没有稳定的发送人来源。如果查询时回源 Knowledge，会产生 N+1 请求，而且 ES 无法从 PostgreSQL 独立重建。

### 4.2 推荐方案

将发送人字段放到 `rag.resource_snapshots`，而不是每条 Chunk 重复保存。

新增字段：

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `sender_identity_id` | `UUID` | 无 | Knowledge 外部身份记录 ID，可为空 |
| `sender_platform` | `VARCHAR(32)` | 无 | `feishu/wecom/wechat` |
| `sender_workspace_key` | `VARCHAR(255)` | `''` | 平台工作区 |
| `sender_external_user_id` | `VARCHAR(255)` | 无 | 平台外部用户 ID，可为空 |
| `sender_mapped_user_id` | `UUID` | 无 | 映射后的内部用户 ID，可为空 |
| `sender_display_name` | `VARCHAR(255)` | 无 | 采集时发送人展示名 |

规则：

- 消息资源使用消息发送人。
- 附件资源继承所属消息的发送人。
- 公共文档、组织文件库等没有明确发送人时允许为空。
- `sender_identity_id`可以代表未映射内部用户的外部身份。
- 只有平台外部 ID 时仍保存平台、工作区和外部用户 ID。
- 外部发送人没有 `sender_mapped_user_id` 时，不影响 RAG 索引和检索。
- `sender_display_name` 是采集快照，不跟随平台后续改名自动变化。
- ES 投影从 `resource_snapshots` 复制这两个字段。
- PostgreSQL Chunk 不重复保存发送人字段。
- 重建 ES 时直接读取 Resource Snapshot。

如果后续需要显示用户当前名称，应由 Core 的身份目录在响应层补充，不能回写不可变快照。

### 4.3 ES Mapping

```text
sender_identity_id: keyword
sender_platform: keyword
sender_display_name: text + keyword subfield
```

用途：

- `sender_identity_id` 用于精确过滤。
- `sender_platform` 用于平台过滤和展示。
- `sender_display_name` 用于 BM25 和展示。

## 5. PG 到 ES 字段映射

### 5.1 Chunk 和来源身份

| ES 字段 | PG 来源 | 生成方式 |
|---|---|---|
| `chunk_id` | `chunks.chunk_id` | 直接复制 |
| `logical_position_key` | `chunks` 组合字段 | 查询侧或投影时计算 |
| `resource_snapshot_id` | `chunks.resource_snapshot_id` | 直接复制 |
| `knowledge_item_id` | `chunks.knowledge_item_id` | 直接复制 |
| `resource_type` | `chunks.resource_type` | 直接复制 |
| `resource_id` | `chunks.resource_id` | 直接复制 |
| `knowledge_base_id` | `chunks.knowledge_base_id` | 直接复制 |
| `document_id` | `chunks.document_id` | 直接复制 |
| `message_id` | `chunks.message_id` | 直接复制 |
| `source_conversation_id` | `chunks.source_conversation_id` | 直接复制 |
| `external_conversation_id` | `resource_snapshots` | 投影时复制 |

### 5.2 Scope、受众和权限

| ES 字段 | PG 来源 | 生成方式 |
|---|---|---|
| `scope_type` | `chunks.scope_type` | 直接复制 |
| `scope_id` | `chunks.scope_id` | 直接复制 |
| `scope_key` | `chunks.scope_key` | 直接复制 |
| `source_conversation_type` | `resource_snapshots` | 投影时复制 |
| `source_audience_policy` | `resource_snapshots` | 投影时复制 |
| `auth_partition_key` | `chunks.auth_partition_key` | 直接复制 |
| `auth_object_key` | `chunks.auth_object_key` | 直接复制 |
| `acl_version` | `chunks.acl_version` | 直接复制 |
| `sensitivity` | `chunks.sensitivity` | 直接复制 |
| `rag_eligible` | `chunks.rag_eligible` | 直接复制 |
| `lifecycle_status` | `chunks.lifecycle_status` | 直接复制 |

### 5.3 内容和版本

| ES 字段 | PG 来源 | 生成方式 |
|---|---|---|
| `content` | `chunks.content` | 直接复制 |
| `content_hash` | `chunks.content_hash` | 直接复制 |
| `content_version` | `chunks.content_version` | 直接复制 |
| `processing_version` | `chunks.processing_version` | 直接复制 |
| `chunking_version` | `chunks.chunking_version` | 直接复制 |
| `content_variant` | `chunks.content_variant` | 直接复制 |
| `chunk_index` | `chunks.chunk_index` | 直接复制 |
| `chunk_count` | `chunks.chunk_count` | 直接复制 |

### 5.4 展示和来源

| ES 字段 | PG 来源 | 生成方式 |
|---|---|---|
| `title` | `chunks.title` | 直接复制 |
| `file_name` | `chunks.file_name` | 直接复制 |
| `heading_path` | `chunks.heading_path` | 直接复制 |
| `sender_identity_id` | `resource_snapshots.sender_identity_id` | 投影时复制 |
| `sender_display_name` | `resource_snapshots.sender_display_name` | 投影时复制 |
| `sent_at` | `chunks.sent_at` | 直接复制 |
| `context_header` | `chunks.context_header` | 直接复制 |
| `source_locator` | `chunks.source_locator` | 直接复制 |

### 5.5 Tree Branch

| ES 字段 | PG 来源 | 生成方式 |
|---|---|---|
| `branch_keys` | `chunk_branches.branch_key` | 按 Chunk 聚合数组 |
| `registry_version` | `chunk_branches.registry_version` | 取当前投影使用的 Registry 版本 |

没有 Branch 时写入空数组。

### 5.6 向量和状态

| ES 字段 | PG 来源 | 生成方式 |
|---|---|---|
| `embedding` | 无 | 重建时调用 Embedding 服务 |
| `embedding_model` | `chunks.embedding_model` | 直接复制 |
| `embedding_dimensions` | `chunks.embedding_dimensions` | 直接复制 |
| `created_at` | `chunks.created_at` | 直接复制 |
| `indexed_at` | `projection_records.indexed_at` | 投影成功后写入 |

## 6. Knowledge 到 PG 的字段映射

### 6.1 事件字段

| 事件字段 | PG 目标 |
|---|---|
| `resource_type` | `processing_jobs.resource_type`、`resource_snapshots.resource_type` |
| `resource_id` | `processing_jobs.resource_id`、`resource_snapshots.resource_id` |
| `knowledge_item_id` | `processing_jobs.knowledge_item_id` |
| `source_conversation_id` | `processing_jobs.source_conversation_id` |
| `source_conversation_type` | `resource_snapshots.source_conversation_type` |
| `source_audience_policy` | `processing_jobs.source_audience_policy` |
| `content_version` | `processing_jobs.content_version` |
| `acl_version` | `processing_jobs.acl_version` |
| `content_hash` | `resource_snapshots.content_hash` |

### 6.2 回源字段

| Knowledge 回源字段 | PG 目标 |
|---|---|
| `knowledge_base_id` | `resource_snapshots.knowledge_base_id` |
| `scope_type` | `resource_snapshots.scope_type` |
| `scope_id` | `resource_snapshots.scope_id` |
| `external_conversation_id` | `resource_snapshots.external_conversation_id` |
| `sender_identity_id` | `resource_snapshots.sender_identity_id` |
| `sender_display_name` | `resource_snapshots.sender_display_name` |
| `object_ref` | `resource_snapshots.object_ref` |
| `content` | `chunks.content` |
| `title` | `chunks.title` |
| `file_name` | `chunks.file_name` |
| `sent_at` | `chunks.sent_at` |

## 7. 投影顺序

```text
1. Dispatcher 写 processing_jobs
2. Parse Lane 回源并写 resource_snapshots
3. Parse Lane 生成并写 chunks，embedding_status=pending
4. Index Lane 计算 logical_position_key 和 branch_keys
5. Index Lane 调用 Embedding
6. Index Lane 写 ES
7. ES refresh=wait_for 成功
8. 更新 projection_records=ready
9. 回调 Knowledge=ready
10. Memory Lane 异步处理候选实体
```

任何阶段失败都不能跳过 Embedding 或 ES 写入直接进入 `ready`。

## 8. 幂等和版本

### 8.1 Chunk 幂等

Chunk 唯一身份：

```text
knowledge_item_id
+ content_version
+ processing_version
+ content_variant
+ chunk_index
```

### 8.2 ES 幂等

```text
_index = display_read/write 或 protected_read/write
_id = chunk_id
```

同一 Chunk 重复写入覆盖旧文档，不创建副本。

### 8.3 旧版本保护

- ES 文档必须包含 `content_version` 和 `processing_version`。
- 旧任务不能覆盖新版本已经写入的文档。
- 重建必须使用 PostgreSQL 当前有效 Chunk。
- 旧 ES 索引保留只读，不再接收新向量。

## 9. 重建规则

### 9.1 ES 重建

```text
读取 PostgreSQL 有效 Chunks
-> 重新生成 Embedding
-> 聚合 chunk_branches
-> 批量写新物理索引
-> 校验数量、维度和抽样结果
-> 切换读写别名
-> 验证新索引
-> 旧索引保留只读
```

- 不迁移旧索引中的现有向量。
- 需要历史数据时重新生成 Embedding。
- 新索引切换后成为唯一写入目标。

### 9.2 不重跑解析

正常 ES 重建不重新解析 Knowledge 原文件。

只有 parsing、chunking 或业务内容模型变化时，才重新回放 Knowledge 内容并生成新的 `processing_version`。

## 10. 字段权威规则

| 字段类别 | 权威来源 |
|---|---|
| 原始资源、消息、附件 | Knowledge |
| 来源会话、发送人、ACL | Knowledge |
| 任务和调度状态 | RAG PostgreSQL |
| Resource Snapshot | RAG PostgreSQL |
| Chunk 正文和定位 | RAG PostgreSQL |
| Entity、Alias、Candidate | RAG PostgreSQL |
| Tree Node、Branch | RAG PostgreSQL |
| BM25 字段 | Elasticsearch 投影 |
| 向量 | Elasticsearch |
| 投影状态 | RAG PostgreSQL |
| 搜索和 QA 历史 | RAG PostgreSQL |

## 11. 已确认决策

### 11.1 发送人快照

发送人保存为采集时的快照：

```text
sender_identity_id
sender_platform
sender_workspace_key
sender_external_user_id
sender_mapped_user_id
sender_display_name
```

后续平台改名不自动修改。

### 11.2 发送人字段位置

发送人字段放在 `resource_snapshots`，Chunk 只投影复制，不在每个 Chunk 重复保存。

### 11.3 向量重建

向量只存 ES，ES 重建时重新调用 Embedding 服务。

### 11.4 向量元数据

PostgreSQL `chunks` 只保存：

```text
embedding_model
embedding_dimensions
embedding_status
```

不保存向量本体。

### 11.5 旧索引处理

不迁移旧索引数据。创建新索引后直接切换 RAG 配置，旧索引保留只读，不参与新查询和新写入。

### 11.6 PG 基础设计回写

发送人字段已补入 `RAG服务MVP数据库表结构设计.md` 的 `resource_snapshots` 表，并已同步到 ES 设计稿。
