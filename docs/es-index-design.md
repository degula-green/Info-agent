# Elasticsearch 索引设计

## 1. 基本约定

- Elasticsearch `9.5.2`，IK 插件版本与 ES 完全一致。
- Embedding 使用 `text-embedding-v4`，维度 `1536`，相似度 `cosine`。
- 每个 chunk 是一条 ES 文档；消息和附件共用字段模型。
- ES 不保存附件二进制、MinIO 地址、访问令牌或 OpenFGA 用户列表。
- ES 只由 RAG 服务访问，最终权限以 Core/OpenFGA 为准。

## 2. 双索引

| 物理索引 | 内容 | 查询条件 |
|---|---|---|
| `knowledge_display_chunks_v1` | 普通/脱敏消息、附件元数据、普通附件解析内容 | 按 owner、organization、conversation group 粗筛，候选再 Check |
| `knowledge_protected_chunks_v1` | 敏感消息原文、受保护附件原始解析内容及其向量 | 必须先取得 OpenFGA 授权对象集合，再执行 BM25/kNN |

别名：

```text
knowledge_display_chunks_read / knowledge_display_chunks_write
knowledge_protected_chunks_read / knowledge_protected_chunks_write
```

受保护附件不做内容脱敏。为支持获批用户的全文检索和 RAG，其原始解析 chunk 与向量进入受保护索引；无权限用户只能在展示索引中看到 `attachment_metadata`。

## 3. OpenFGA 对齐

ES 使用外部 Access Check 语义，并保存对应的 OpenFGA 对象键：

| `part_kind` | 索引 | `auth_resource_type` | `auth_resource_part` | `auth_resource_id` | `auth_object_key` |
|---|---|---|---|---|---|
| `message_display` | display | `knowledge_item` | `display` | `knowledge_item_id` | `knowledge_item:{id}` |
| `knowledge_original` | protected | `knowledge_item` | `original` | `knowledge_item_id` | `knowledge_original:{id}` |
| `attachment_metadata` | display | `attachment` | `metadata` | `attachment_id` | `attachment_meta:{id}` |
| `attachment_content`（普通） | display | `attachment` | `content` | `attachment_id` | `attachment_content:{id}` |
| `attachment_content`（受保护） | protected | `attachment` | `content` | `attachment_id` | `attachment_content:{id}` |

`message_id` 只用于来源追踪，不用于鉴权。消息展示内容统一按 `knowledge_item:{knowledge_item_id}` 检查，避免在 Core 中维护 `message_id → knowledge_item_id` 映射。

OpenFGA 关系保持现有定义：

- `knowledge_item.view`：owner、组织成员或有效群聊成员。
- `knowledge_original.view`：显式 owner/viewer 且父 `knowledge_item` 仍可见。
- `attachment_meta.view`：owner、组织成员或有效群聊成员。
- 普通 `attachment_content.view`：基础范围 `accessor` 且父元数据仍可见。
- 受保护 `attachment_content.view`：显式 `viewer` 且父元数据仍可见。
- `download` 独立检查，拥有 `view` 不自动获得下载权限。

## 4. 查询权限

### 展示索引

```text
owner/organization/conversation_group 粗筛
→ BM25 + kNN
→ 按 auth_resource_type/id/part 批量 Check(view)
→ 删除无权 chunk
→ RRF
→ 按 knowledge_item_id 聚合
→ 返回结果或构造 RAG 上下文
```

### 受保护索引

```text
Core/OpenFGA ListObjects(view)
→ 得到 knowledge_original:* / attachment_content:* 对象集合
→ 作为 auth_object_key terms filter 执行 BM25 + kNN
→ 对命中对象再次批量 Check(view)
→ RRF 和聚合
→ 已授权原文进入 RAG 上下文
```

禁止先对全部受保护向量召回、再过滤权限。若授权对象过多，一期优先使用单资源 RAG；跨资源查询可由 Core 生成短期授权集合，并通过 ES terms lookup 预过滤。

应用层 RRF 不使用单一 ES `search_after`。混合搜索分页使用 Redis 保存 PIT、BM25 游标、有限向量候选和已返回结果；纯 BM25 可直接使用 PIT + `search_after`。

## 5. 字段

| 分类 | 字段 |
|---|---|
| Chunk | `chunk_id`、`chunk_index`、`chunk_count`、`chunking_version` |
| 资源 | `knowledge_item_id`、`attachment_id`、`knowledge_base_id` |
| 版本 | `content_version`、`attachment_content_version`、`auth_acl_version`、`mapping_version` |
| 鉴权 | `auth_resource_type`、`auth_resource_part`、`auth_resource_id`、`auth_object_key` |
| 范围 | `knowledge_scope`、`access_scope`、`organization_id`、`owner_user_id`、`conversation_group_id` |
| 内容 | `part_kind`、`content_access_required`、`title`、`content`、`content_hash`、`content_visibility` |
| 向量 | `embedding`、`embedding_model`、`vectorized`、`rag_eligible` |
| 来源 | `conversation_ingestion_id`、`external_conversation_id`、`message_id`、`external_message_id`、`sender_identity_id`、`sender_display_name`、`sent_at` |
| 附件 | `file_name`、`mime_type`、`size_bytes`、`source_locator` |
| 状态 | `sensitivity`、`lifecycle_status`、`indexed_at`、`created_at` |

`auth_acl_version` 始终对应当前 chunk 的鉴权对象：消息取知识项 ACL 版本，附件元数据和内容取附件 ACL 版本。它只用于版本校验，不能代替实时授权。

稳定文档 ID：

```text
sha256(knowledge_item_id + content_version + part_kind
       + attachment_id-or-empty + attachment_content_version-or-zero
       + chunking_version + chunk_index)
```

## 6. Mapping v1

两个物理索引共用以下 Mapping，分别配置自己的读写别名：

```json
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  },
  "mappings": {
    "dynamic": "strict",
    "_source": {"excludes": ["embedding"]},
    "properties": {
      "chunk_id": {"type": "keyword"},
      "chunk_index": {"type": "integer"},
      "chunk_count": {"type": "integer"},
      "chunking_version": {"type": "keyword"},

      "knowledge_item_id": {"type": "keyword"},
      "attachment_id": {"type": "keyword"},
      "knowledge_base_id": {"type": "keyword"},
      "content_version": {"type": "integer"},
      "attachment_content_version": {"type": "integer"},
      "auth_acl_version": {"type": "long"},
      "mapping_version": {"type": "keyword"},

      "auth_resource_type": {"type": "keyword"},
      "auth_resource_part": {"type": "keyword"},
      "auth_resource_id": {"type": "keyword"},
      "auth_object_key": {"type": "keyword"},

      "knowledge_scope": {"type": "keyword"},
      "access_scope": {"type": "keyword"},
      "organization_id": {"type": "keyword"},
      "owner_user_id": {"type": "keyword"},
      "conversation_group_id": {"type": "keyword"},

      "part_kind": {"type": "keyword"},
      "content_access_required": {"type": "boolean"},
      "title": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}
      },
      "content": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },
      "content_hash": {"type": "keyword"},
      "content_visibility": {"type": "keyword"},
      "embedding": {
        "type": "dense_vector",
        "dims": 1536,
        "index": true,
        "similarity": "cosine",
        "index_options": {"type": "hnsw", "m": 16, "ef_construction": 100}
      },
      "embedding_model": {"type": "keyword"},
      "vectorized": {"type": "boolean"},
      "rag_eligible": {"type": "boolean"},

      "conversation_ingestion_id": {"type": "keyword"},
      "external_conversation_id": {"type": "keyword"},
      "message_id": {"type": "keyword"},
      "external_message_id": {"type": "keyword"},
      "sender_identity_id": {"type": "keyword"},
      "sender_display_name": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
      },
      "sent_at": {"type": "date"},
      "file_name": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}}
      },
      "mime_type": {"type": "keyword"},
      "size_bytes": {"type": "long"},
      "source_locator": {
        "type": "object",
        "dynamic": "strict",
        "properties": {
          "page_number": {"type": "integer"},
          "paragraph_index": {"type": "integer"},
          "slide_number": {"type": "integer"},
          "sheet_name": {"type": "keyword", "ignore_above": 512},
          "row_start": {"type": "integer"},
          "row_end": {"type": "integer"},
          "column_start": {"type": "integer"},
          "column_end": {"type": "integer"},
          "char_start": {"type": "integer"},
          "char_end": {"type": "integer"}
        }
      },

      "sensitivity": {"type": "keyword"},
      "lifecycle_status": {"type": "keyword"},
      "indexed_at": {"type": "date"},
      "created_at": {"type": "date"}
    }
  }
}
```

## 7. 写入校验

- `embedding_model=text-embedding-v4`，向量长度必须为 `1536`。
- `mapping_version=v1`，`dynamic=strict`。
- `attachment_metadata` 不写 `embedding`，并设置 `vectorized=false`、`rag_eligible=false`。
- 展示索引只允许 `message_display`、`attachment_metadata` 和普通 `attachment_content`。
- 受保护索引只允许 `knowledge_original` 和 `content_access_required=true` 的 `attachment_content`。
- 受保护索引查询必须带非空 `auth_object_key` 授权过滤条件；为空时直接返回空结果。
- 原始文件、短时 URL、Token 和解析器临时字段不得写入 ES 或日志。
- 索引初始化前必须验证 ES 版本、IK analyzer 和 Mapping 中的向量维度。

## 8. 运行要求

- 两类索引使用不同 ES 角色；普通检索账号不能读取受保护索引。
- ES 仅开放给内部网络，生产启用认证、TLS、磁盘和备份加密。
- 受保护索引查询、候选二次 Check、RAG 使用、预览和下载均记录安全审计。
- Mapping、IK 配置或向量模型变化时创建新物理索引，重建后原子切换别名。
