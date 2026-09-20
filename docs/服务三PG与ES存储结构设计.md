# 服务三 PG 与 ES 存储结构设计

> 一期设计稿。服务二/MinIO 保存原始内容、附件、归属、权限和版本；服务三保存派生记忆与检索投影。

## 1. 存储分工

| 数据 | MinIO/服务二 | 服务三 PG | 服务三 ES |
|---|---|---|---|
| 原始消息、附件、解析产物 | 权威保存 | 仅保存引用 | 不保存原件 |
| Chunk | 来源内容 | 保存正文、哈希、定位 | 沿用现有 Chunk 索引投影 |
| Fact、版本、树关系 | 不保存 | 权威保存 | Fact/Node 检索投影 |
| 摘要 | 不保存 | display/protected 两套摘要 | 全文和向量投影 |
| Embedding | 不保存 | 保存状态和模型版本 | 保存向量 |
| 任务和索引状态 | 不保存 | `processing_jobs`、`index_records` | 不作为状态源 |

服务三不直接读取服务二数据库，也不复制完整消息、附件或原始文件。通过 `knowledge.ready` 事件和 HTTP 回源指定版本。

## 2. PostgreSQL 设计

所有表位于 `rag` Schema，主键使用 UUID，时间使用 `TIMESTAMPTZ`，跨服务只保存逻辑 ID，不建立跨 Schema 业务外键。

通用权限/版本字段：

```text
knowledge_base_id, organization_id, owner_user_id
content_version, content_hash, acl_version
visibility, access_scope, sensitivity, auth_object_key
```

`auth_object_key` 只用于 protected ES 候选过滤，不能替代实时 OpenFGA。

### 2.1 `rag.memory_sources`

统一表示服务二资源的版本化处理快照，不复制原文。

```text
id UUID PRIMARY KEY
source_type VARCHAR(32) NOT NULL       -- message / attachment / knowledge_item
source_id UUID NOT NULL
knowledge_item_id UUID NOT NULL
message_id UUID
attachment_id UUID
knowledge_base_id UUID NOT NULL
organization_id UUID
owner_user_id UUID
source_ref VARCHAR(512)
content_version INTEGER NOT NULL
content_hash CHAR(64) NOT NULL
acl_version BIGINT NOT NULL DEFAULT 0
visibility VARCHAR(16)
access_scope VARCHAR(32)
sensitivity VARCHAR(32)
auth_object_key VARCHAR(512)
processing_version VARCHAR(64) NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

唯一键：`source_type + source_id + content_version + processing_version`。建立 `knowledge_item_id/content_version`、`attachment_id/content_version` 和 `auth_object_key` 索引。

### 2.2 `rag.memory_chunks`

一期长期保存 Chunk 正文和来源定位；完整附件和解析产物仍在 MinIO。

```text
id UUID PRIMARY KEY
source_id UUID NOT NULL
knowledge_item_id UUID NOT NULL
attachment_id UUID
content_version INTEGER NOT NULL
chunk_index INTEGER NOT NULL
chunk_count INTEGER NOT NULL
chunk_text TEXT NOT NULL
content_hash CHAR(64) NOT NULL
chunking_version VARCHAR(64) NOT NULL
page_number INTEGER
paragraph_index INTEGER
slide_number INTEGER
sheet_name VARCHAR(512)
row_start INTEGER, row_end INTEGER
column_start INTEGER, column_end INTEGER
char_start INTEGER, char_end INTEGER
visibility VARCHAR(16)
access_scope VARCHAR(32)
sensitivity VARCHAR(32)
auth_object_key VARCHAR(512)
acl_version BIGINT NOT NULL DEFAULT 0
embedding_status VARCHAR(32) NOT NULL DEFAULT 'pending'
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

唯一键：`source_id + chunking_version + chunk_index`。

### 2.3 `rag.memory_facts`

保存稳定的语义事实身份；事实文本和历史值存放在版本表。

```text
id UUID PRIMARY KEY
knowledge_base_id UUID NOT NULL
organization_id UUID
owner_user_id UUID
fact_type VARCHAR(32) NOT NULL       -- event / state / task / relation
dedupe_key CHAR(64) NOT NULL
subject_entity_id UUID
predicate VARCHAR(256)
current_version_id UUID
status VARCHAR(32) NOT NULL           -- active / superseded / revoked / historical / conflict
visibility VARCHAR(16)
access_scope VARCHAR(32)
sensitivity VARCHAR(32)
auth_object_key VARCHAR(512)
acl_version BIGINT NOT NULL DEFAULT 0
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

去重键由 `fact_type + 主体 + predicate + 规范化值 + 时间窗口` 生成。唯一键：`knowledge_base_id + dedupe_key`。

### 2.4 `rag.memory_fact_versions`

保存同一语义 Fact 的历史、覆盖、撤销和冲突版本。

```text
id UUID PRIMARY KEY
fact_id UUID NOT NULL
version_no INTEGER NOT NULL
fact_text TEXT NOT NULL
normalized_value JSONB
valid_from TIMESTAMPTZ
valid_to TIMESTAMPTZ
status VARCHAR(32) NOT NULL
supersedes_version_id UUID
revoke_reason TEXT
confidence NUMERIC(5,4)
extraction_version VARCHAR(64)
content_version INTEGER NOT NULL
visibility VARCHAR(16)
access_scope VARCHAR(32)
sensitivity VARCHAR(32)
auth_object_key VARCHAR(512)
acl_version BIGINT NOT NULL DEFAULT 0
created_at TIMESTAMPTZ NOT NULL
```

唯一键：`fact_id + version_no`。普通查询默认使用 `memory_facts.current_version_id` 对应的 `active` 版本。

### 2.5 事实来源关系

`rag.memory_fact_chunks` 保存 Fact 版本到 Chunk 的多对多关系：

```text
fact_id UUID NOT NULL
fact_version_id UUID NOT NULL
chunk_id UUID NOT NULL
relation_type VARCHAR(32) NOT NULL   -- evidence / support / contradiction
evidence_text TEXT
confidence NUMERIC(5,4)
created_at TIMESTAMPTZ NOT NULL
PRIMARY KEY (fact_version_id, chunk_id)
```

同一 Fact 可以来自多条消息或多个附件，Fact 正文只保存一份。

### 2.6 `rag.memory_trees`

```text
id UUID PRIMARY KEY
knowledge_base_id UUID NOT NULL
organization_id UUID
owner_user_id UUID
tree_type VARCHAR(16) NOT NULL       -- session / entity / scene
subject_id UUID NOT NULL
root_node_id UUID
strategy_version VARCHAR(64) NOT NULL
current_version BIGINT NOT NULL DEFAULT 1
status VARCHAR(32) NOT NULL DEFAULT 'active'
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

唯一键：`knowledge_base_id + tree_type + subject_id`。一期自动复用 Session Tree 和 Entity Tree，Scene Tree 只预留。

### 2.7 `rag.memory_nodes`

Root、Internal Node、Leaf 使用统一表，采用 `parent_id + level` 邻接表模型。

```text
id UUID PRIMARY KEY
tree_id UUID NOT NULL
parent_id UUID
node_type VARCHAR(16) NOT NULL      -- root / internal / leaf
level INTEGER NOT NULL
node_key VARCHAR(256) NOT NULL
topic_key VARCHAR(256)
phase_key VARCHAR(256)
time_start TIMESTAMPTZ
time_end TIMESTAMPTZ
display_summary TEXT
protected_summary TEXT
summary_version INTEGER NOT NULL DEFAULT 0
summary_strategy_version VARCHAR(64)
content_version BIGINT NOT NULL DEFAULT 1
dirty BOOLEAN NOT NULL DEFAULT TRUE
status VARCHAR(32) NOT NULL DEFAULT 'active'
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

约束和索引：`UNIQUE(tree_id, node_key)`、`INDEX(tree_id, parent_id, level)`、`INDEX(tree_id, node_type)`。Root 的 `parent_id` 为空；不同权限边界不得混入同一 Leaf。

### 2.8 `rag.memory_node_facts`

```text
node_id UUID NOT NULL
fact_id UUID NOT NULL
fact_version_id UUID NOT NULL
position INTEGER
relation_type VARCHAR(32) NOT NULL DEFAULT 'primary'
mount_strategy_version VARCHAR(64)
status VARCHAR(16) NOT NULL DEFAULT 'active'
created_at TIMESTAMPTZ NOT NULL
PRIMARY KEY (node_id, fact_version_id)
```

应用层保证 `node_id` 指向 Leaf。同一个 Fact 可挂载到多棵 Tree 和多个 Leaf。

### 2.9 `rag.memory_entities`

```text
id UUID PRIMARY KEY
knowledge_base_id UUID NOT NULL
organization_id UUID
entity_type VARCHAR(64) NOT NULL
canonical_name VARCHAR(512) NOT NULL
normalized_key VARCHAR(512) NOT NULL
aliases JSONB NOT NULL DEFAULT '[]'::jsonb
merge_status VARCHAR(32) NOT NULL DEFAULT 'active'
confidence NUMERIC(5,4)
recognition_version VARCHAR(64)
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
UNIQUE (knowledge_base_id, entity_type, normalized_key)
```

### 2.10 既有任务和投影状态表

扩展既有 `rag.processing_jobs`，支持 `chunk`、`fact`、`fact_version`、`tree_update`、`summary`、`embedding`、`index` 阶段，并携带资源版本、处理版本、ACL 版本、重试和错误信息。

扩展既有 `rag.index_records` 为通用投影状态：

```text
object_type VARCHAR(16)              -- chunk / fact / node
object_id UUID NOT NULL
variant VARCHAR(16) NOT NULL         -- display / protected
es_index_alias VARCHAR(256) NOT NULL
es_document_id VARCHAR(512) NOT NULL
content_version INTEGER NOT NULL
acl_version BIGINT NOT NULL
mapping_version VARCHAR(64) NOT NULL
embedding_model VARCHAR(128)
status VARCHAR(32) NOT NULL
indexed_at TIMESTAMPTZ
last_error TEXT
```

## 3. Elasticsearch 设计

### 3.1 物理索引

```text
memory_display_facts_v1
memory_protected_facts_v1
memory_display_nodes_v1
memory_protected_nodes_v1
```

现有 `knowledge_display_chunks_v1` 和 `knowledge_protected_chunks_v1` 继续使用。每组索引配置独立读写别名。

### 3.2 Fact 文档粒度

采用“一条 Fact-Tree/Leaf 挂载一条文档”。PG 只保存一份 Fact 正文，ES 通过 `fact_projection_id` 区分不同 Tree/Leaf 挂载。

Fact 字段：

```text
fact_projection_id, fact_id, fact_version_id
tree_id, tree_type, node_id, parent_id, level, node_type
knowledge_base_id, organization_id, owner_user_id
subject_entity_id, fact_type, fact_text, dedupe_key, fact_status
valid_from, valid_to, source_chunk_ids
knowledge_item_id, attachment_id
visibility, access_scope, sensitivity, auth_object_key
acl_version, content_version
embedding(dense_vector, dims=1536, cosine)
embedding_model, mapping_version, lifecycle_status
created_at, indexed_at
```

### 3.3 Node 文档粒度

Root、Internal Node、Leaf 共用 Node 索引，每个节点一条文档，通过 `node_type` 区分。

```text
node_id, tree_id, tree_type, node_type, parent_id, level
knowledge_base_id, organization_id, owner_user_id, subject_id
topic_key, phase_key, time_start, time_end
summary, summary_version, summary_strategy_version, content_version
visibility, access_scope, sensitivity, auth_object_key, acl_version
embedding(dense_vector, dims=1536, cosine)
embedding_model, mapping_version, lifecycle_status
created_at, indexed_at
```

display Node 使用 `display_summary`；protected Node 使用 `protected_summary`。

### 3.4 Mapping 与投影规则

Fact/Node 索引使用 `dynamic: strict`，向量模型一期为 `text-embedding-v4`，维度 `1536`，相似度 `cosine`，HNSW 参数与现有 Chunk mapping 保持一致。

投影规则：

- display 只写公开、脱敏或基础可见内容；
- protected 只写受保护 Fact/Node；
- `fact_status` 非 `active` 时默认不参与当前检索；
- ES 文档缺失可由 PG 和服务二来源重建；
- ES 不保存二进制、Token、长期下载 URL 或 OpenFGA 用户列表。

Fact 文档 ID：

```text
sha256(fact_id + fact_version_id + tree_id + node_id + mapping_version)
```

Node 文档 ID：

```text
sha256(node_id + summary_version + variant + mapping_version)
```

## 4. 写入与同步

PG 事务内完成：

```text
memory_sources
→ memory_chunks
→ memory_facts / memory_fact_versions
→ memory_fact_chunks
→ memory_trees / memory_nodes
→ memory_node_facts
→ dirty 标记
→ processing_jobs / index_records
```

PG 提交后异步执行摘要、Embedding 和 ES upsert，成功后更新 `index_records` 并发布 `processing.completed`。ES 失败不回滚 PG 关系，依赖任务重试或从 PG 重建。

处理主键：

```text
knowledge_item_id + content_version + processing_version
attachment_id + content_version + parsing_version
```

旧版本任务不能覆盖新版本 Chunk、Fact、摘要或 ES 文档。

## 5. 查询与权限

```text
Query Understanding
→ 限定空间和 Knowledge Base
→ Root 检索
→ Node 逐层下钻
→ Leaf/Fact 检索
→ 获取 Fact-Chunk 关系
→ 回源权威上下文
→ 最终权限复核
```

protected 查询流程：

```text
OpenFGA ListObjects(view)
→ 得到授权 auth_object_key 集合
→ protected ES terms 过滤
→ BM25/kNN 召回
→ ACL 版本校验
→ 回源前再次 Check(view)
```

授权集合为空时 protected 查询直接返回空结果；禁止先全量向量召回再过滤权限。

## 6. 验收标准

1. 重复事件不重复创建 Source、Chunk、Fact、挂载或 ES 文档。
2. 同一 Fact 来自多个消息/附件时，PG 只保存一份事实正文，来源关系完整。
3. 同一 Fact 可同时挂载 Session 和 Entity 多个 Leaf。
4. `parent_id + level` 能完成 Root → Node → Leaf 下钻。
5. Fact 覆盖、撤销和冲突保留历史，默认只返回当前有效版本。
6. 敏感 Fact 只进入 protected 索引，无授权时不可召回。
7. 公开 Node 摘要不包含受限 Leaf 的具体敏感信息。
8. ES 失败可依据任务和投影状态重试，PG 可重建 ES。
9. 私人空间数据不能被组织空间查询到。

