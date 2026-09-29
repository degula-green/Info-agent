# RAG 服务 MVP Elasticsearch 索引设计

> 状态：已确认设计基线
>
> 配套文档：`RAG服务MVP数据库表结构设计.md`
>
> 范围：`services/rag` 的 Elasticsearch Chunk 检索投影

## 1. 设计目标

- Elasticsearch 只保存可重建的 Chunk 检索投影。
- PostgreSQL 是 Chunk、Entity、Branch 和投影状态的权威来源。
- display 和 protected 使用独立物理索引，避免原敏感正文进入普通检索。
- 一个 Chunk 变体对应一个 ES 文档。
- 一个文档同时支持 BM25、kNN 和确定性 metadata/branch 过滤。
- display/protected 同一逻辑位置可以在查询侧去重。
- Tree Node、Fact、Entity 和 Candidate 不进入 MVP ES 索引。

## 2. 索引拓扑

### 2.1 物理索引

```text
rag_chunks_display_v1
rag_chunks_protected_v1
```

用途：

| 索引 | 用途 |
|---|---|
| `rag_chunks_display_v1` | 普通内容和脱敏内容 |
| `rag_chunks_protected_v1` | 原始敏感正文和受保护附件正文 |

### 2.2 读写别名

```text
rag_chunks_display_read
rag_chunks_display_write

rag_chunks_protected_read
rag_chunks_protected_write
```

- RAG 写入使用 `_write` 别名。
- 搜索读取使用 `_read` 别名。
- mapping 升级时创建新的物理版本，例如 `_v2`。
- 新索引回填完成后切换读别名。
- 不在已有物理索引上修改字段类型。

### 2.3 明确不创建的索引

MVP 不创建：

```text
memory_nodes_*
memory_facts_*
entity_search_*
entity_candidate_*
tree_node_*
```

Tree Node、Entity、Alias 和 Candidate 保存在 PostgreSQL。树只通过 Chunk 的 `branch_keys` 影响 Chunk 检索。

## 3. 通用 Mapping 原则

```json
{
  "dynamic": "strict",
  "date_detection": false,
  "_source": {
    "excludes": ["embedding"]
  }
}
```

- 所有字段必须显式 mapping。
- 不允许动态字段进入正式索引。
- `embedding` 参与 kNN，但默认不返回 `_source`。
- 正文使用 IK 分词。
- ID、枚举、Scope、权限分区和 Branch Key 使用 `keyword`。
- 日期统一使用 ISO-8601。

## 4. 字段设计

### 4.1 Chunk 和来源身份

| 字段 | ES 类型 | 是否索引 | 用途 |
|---|---|---|---|
| `chunk_id` | `keyword` | 是 | 稳定 Chunk 检索 ID，同时可作为 `_id` |
| `logical_position_key` | `keyword` | 是 | display/protected 逻辑位置去重键 |
| `resource_snapshot_id` | `keyword` | 是 | 对应 PG 资源快照 |
| `knowledge_item_id` | `keyword` | 是 | KnowledgeItem ID |
| `resource_type` | `keyword` | 是 | `message` 或 `attachment` |
| `resource_id` | `keyword` | 是 | 消息或附件 ID |
| `knowledge_base_id` | `keyword` | 是 | 知识库过滤 |
| `document_id` | `keyword` | 是 | 文档资源过滤 |
| `message_id` | `keyword` | 是 | 消息定位 |
| `source_conversation_id` | `keyword` | 是 | 内部来源会话 |
| `external_conversation_id` | `keyword` | 是 | 外部平台会话展示和排障 |

`logical_position_key` 建议由以下字段生成：

```text
sha256(
  knowledge_item_id
  + content_version
  + processing_version
  + chunking_version
  + chunk_index
)
```

它只存在于 ES 投影和查询阶段，不进入 PostgreSQL。

### 4.2 Scope、受众和权限

| 字段 | ES 类型 | 是否索引 | 用途 |
|---|---|---|---|
| `scope_type` | `keyword` | 是 | `organization/user` |
| `scope_id` | `keyword` | 是 | Scope ID |
| `scope_key` | `keyword` | 是 | Scope 精确隔离 |
| `source_conversation_type` | `keyword` | 是 | `group/private` |
| `source_audience_policy` | `keyword` | 是 | 来源受众策略 |
| `auth_partition_key` | `keyword` | 是 | 分支权限分区过滤 |
| `auth_object_key` | `keyword` | 是 | protected 精确对象过滤 |
| `acl_version` | `long` | 是 | 权限版本诊断 |
| `sensitivity` | `keyword` | 是 | 敏感等级过滤和诊断 |
| `rag_eligible` | `boolean` | 是 | 是否允许进入 AI/RAG |
| `lifecycle_status` | `keyword` | 是 | 生命周期过滤 |

权限过滤字段必须进入 `filter`，不能参与相关性评分。

### 4.3 内容和版本

| 字段 | ES 类型 | 是否索引 | 用途 |
|---|---|---|---|
| `content` | `text`，IK | 是 | BM25 正文检索 |
| `content_hash` | `keyword` | 是 | 内容一致性 |
| `content_version` | `integer` | 是 | 内容版本过滤 |
| `processing_version` | `keyword` | 是 | RAG 处理版本过滤 |
| `chunking_version` | `keyword` | 是 | 切块版本过滤 |
| `content_variant` | `keyword` | 是 | `display/protected` |
| `chunk_index` | `integer` | 是 | 顺序、邻近扩展和诊断 |
| `chunk_count` | `integer` | 是 | 来源 Chunk 总数 |
| `rag_eligible` | `boolean` | 是 | 是否允许进入向量和 AI |

### 4.4 展示和来源定位

| 字段 | ES 类型 | 是否索引 | 用途 |
|---|---|---|---|
| `title` | `text` + `keyword` 子字段 | 是 | 标题检索和展示 |
| `file_name` | `text` + `keyword` 子字段 | 是 | 文件名检索和展示 |
| `heading_path` | `text` + `keyword` 子字段 | 是 | 章节路径检索和展示 |
| `sender_identity_id` | `keyword` | 是 | 发送人外部身份记录 ID |
| `sender_platform` | `keyword` | 是 | 发送人平台 |
| `sender_display_name` | `text` + `keyword` 子字段 | 是 | 发送人检索 |
| `sent_at` | `date` | 是 | 时间过滤和排序 |
| `context_header` | `object`，strict | 是 | 结构化检索上下文 |
| `source_locator` | `object`，strict | 是 | 页、段落、表格定位 |

`context_header` 建议字段：

```text
title
file_name
heading_path
source_kind
```

`source_locator` 建议字段：

```text
page_number
paragraph_index
slide_number
sheet_name
row_start
row_end
column_start
column_end
char_start
char_end
```

### 4.5 树分支

| 字段 | ES 类型 | 是否索引 | 用途 |
|---|---|---|---|
| `branch_keys` | `keyword[]` | 是 | 正式 Entity 分支过滤 |
| `registry_version` | `long` | 是 | 分支使用的 Registry 版本 |

示例：

```json
{
  "branch_keys": [
    "entity:project:8f6b...:2026-09",
    "entity:person:61a2...:2026-09"
  ]
}
```

- Chunk 可以没有 Branch Key。
- 没有 Branch Key 不影响普通检索。
- 候选 Entity 不进入 `branch_keys`。

### 4.6 向量

```json
{
  "embedding": {
    "type": "dense_vector",
    "dims": 1536,
    "index": true,
    "similarity": "cosine",
    "index_options": {
      "type": "hnsw",
      "m": 16,
      "ef_construction": 100
    }
  },
  "embedding_model": {
    "type": "keyword"
  },
  "embedding_dimensions": {
    "type": "integer"
  }
}
```

建议初始：

```text
model: text-embedding-v4
dims: 1536
similarity: cosine
```

### 4.7 时间和投影状态

| 字段 | ES 类型 | 是否索引 | 用途 |
|---|---|---|---|
| `created_at` | `date` | 是 | 数据创建时间 |
| `indexed_at` | `date` | 是 | 投影写入时间 |
| `embedding_model` | `keyword` | 是 | 模型诊断 |
| `embedding_dimensions` | `integer` | 是 | 向量维度诊断 |

## 5. 文档 ID 和幂等

建议 ES 文档 `_id` 直接使用：

```text
chunk_id
```

规则：

- 同一 PG Chunk 重复写入使用相同 `_id`。
- `index` 操作覆盖旧文档，不产生重复。
- display/protected 因为 `content_variant` 不同，`chunk_id` 不同。
- 写别名和 `_id` 共同保证幂等。

## 6. 查询用途

### 6.1 BM25

推荐字段：

```text
title^3
file_name^2
heading_path
content
```

查询使用 `operator=or`，避免自然语言问题因为要求全部词匹配而返回空结果。

### 6.2 kNN

```text
field=embedding
similarity=cosine
filter=权限、Scope、生命周期
```

### 6.3 过滤字段

必须进入 `filter`：

```text
scope_key
knowledge_base_id
content_variant
lifecycle_status
rag_eligible
source_conversation_id
sent_at
branch_keys
auth_partition_key
auth_object_key
```

### 6.4 display/protected 查询

- 普通用户只查询 display 读别名。
- 有权用户同时查询 display 和 protected。
- protected 必须带 `auth_object_key` 精确集合或合法权限分区。
- 两类结果在 RRF 前按 `logical_position_key` 去重。
- 同一逻辑位置有权时保留 protected，无权时保留 display。

## 7. 写入流程

```text
Parse Lane
  -> 写入 PostgreSQL Chunk

Index Lane
  -> Embedding
  -> 计算 branch_keys 和 logical_position_key
  -> 根据 content_variant 选择 write alias
  -> Bulk index
  -> refresh=wait_for
  -> 更新 projection_records
  -> 更新 processing_jobs.status=ready
```

规则：

- `embedding` 不返回给前端。
- `auth_object_key` 不返回给前端。
- Bulk 中任一必要文档失败时，本阶段失败并按 Index Lane 重试。
- ES 失败不回滚 PostgreSQL Chunk。
- `ready` 只能在 ES refresh 成功后回调。

## 8. 重建和 Mapping 升级

```text
创建新版物理索引
-> 写入 mapping
-> 不从旧索引迁移数据
-> 由新写入链路填充，或从 Knowledge/PostgreSQL 回放
-> 校验文档数量和抽样结果
-> 切换 read/write alias 和 RAG 配置
-> 验证新读路径
-> 旧物理索引保留只读
```

规则：

- 不修改已有字段类型。
- 不在旧索引上强行兼容新 mapping。
- 不迁移旧 ES 索引中的文档。
- 新索引可以先为空，由新写入链路逐步填充。
- 如果历史数据需要检索，从 Knowledge/PostgreSQL 回放，不复制旧 ES 文档。
- 旧索引保留只读，不参与新查询和新写入。
- 旧索引不删除，后续清理另行安排。

## 9. 旧索引保留

Phase 7 切换完成后保留：

```text
knowledge_display_chunks_read/write
knowledge_protected_chunks_read/write
memory_display_nodes_read/write
memory_protected_nodes_read/write
memory_display_facts_read/write
memory_protected_facts_read/write
```

规则：

- 不进行新旧索引双写。
- 直接将 RAG 读写配置切换到新索引。
- 不从旧索引迁移文档。
- 旧索引设置为只读，不再参与新查询和新写入。
- 旧索引暂时保留，后续是否删除由运维或 Phase 7 清理阶段决定。
- 如果历史数据需要进入新索引，从 Knowledge/PostgreSQL 回放。

## 10. 已确认决策

### 10.1 物理索引名称

确认使用：

```text
rag_chunks_display_v1
rag_chunks_protected_v1
```

### 10.2 读写别名

确认使用：

```text
rag_chunks_display_read/write
rag_chunks_protected_read/write
```

### 10.3 Display/Protected mapping

确认使用同一套 mapping，通过 `content_variant` 区分内容。

### 10.4 `logical_position_key`

确认 ES 保存该派生字段，用于 display/protected 去重；PostgreSQL 不保存。

### 10.5 中文分词

确认使用：

```text
index analyzer: ik_max_word
search analyzer: ik_smart
```

### 10.6 `branch_keys`

确认使用 `keyword[]`，不建立节点文档。

### 10.7 Embedding

确认使用：

```text
text-embedding-v4
1536 dims
cosine
HNSW m=16, ef_construction=100
```

### 10.8 `_source`

确认排除 `embedding`；`auth_object_key` 内部保留，但 API 返回前移除。

### 10.9 `context_header` 和 `source_locator`

确认使用 `dynamic: strict` 的对象结构，字段固定。

### 10.10 Shard 和 Replica

确认初始使用：

```text
number_of_shards=1
number_of_replicas=0
```

正式扩容时再根据实际负载调整。

### 10.11 Refresh

确认写入使用 `refresh=wait_for`，只有 refresh 完成后才把任务置为 `ready`。

### 10.12 Mapping 升级

确认创建 `_v2` 新索引、回填、切换读别名，不原地修改 mapping。

### 10.13 旧索引清理

确认不迁移旧索引数据，创建新索引后直接切换 RAG 配置；旧 `knowledge_*chunks` 和 `memory_*` 索引保留只读，不参与新查询和新写入。

### 10.14 `metadata_only`

确认不进入 ES，不创建 Chunk，只由 Knowledge 元数据目录展示。
