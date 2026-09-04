# Elasticsearch 索引设计

## 1. 目标与边界

- Elasticsearch 固定使用 `9.5.2`，第一期单节点部署。
- IK 插件版本必须与 Elasticsearch `9.5.2` 完全匹配，不使用自定义业务词典。
- 使用 `knowledge_chunks_read` / `knowledge_chunks_write` 别名访问物理索引。
- 使用一套统一 chunk 索引保存消息和附件的可检索内容，不直接索引会话、搜索历史或历史问答。
- 普通正文、脱敏展示正文和安全展示元数据可以写入 ES；敏感原文、附件二进制、MinIO 短时 URL 和永久对象凭证不得写入 ES。
- 敏感消息和受保护附件允许使用原始解析文本生成向量，但 ES 的 `content` 只能保存脱敏展示文本。向量只能由 RAG 服务访问，不向客户端返回。
- ES 只做候选召回和作用域粗筛。用户能否查看搜索结果、原文、附件内容或将其用于 RAG，最终由 Core/OpenFGA 实时检查。

## 2. 文档模型

一条 ES 文档代表一个 chunk。同一消息正文及其附件可以归属于同一 `knowledge_item_id`，通过 `part_kind` 和独立鉴权资源区分。

| `part_kind` | ES 中的 `content` | `embedding` | 鉴权资源 | RAG 上下文 |
|---|---|---|---|---|
| `message_display` | 普通或脱敏消息正文 | 普通内容使用展示正文；敏感内容可以使用原文 | 有 `source_message_id` 时使用 `message + display + view`，否则使用 `knowledge_item + display + view` | 默认使用展示正文；通过 `original + view` 后可从 Knowledge 服务回源原文 |
| `attachment_metadata` | 可展示的文件名和安全元数据 | 不生成 | `attachment + metadata + view` | 不进入 RAG 上下文 |
| `attachment_display` | 普通或脱敏附件解析文本 | 普通内容使用展示正文；受保护内容可以使用原始解析文本 | `attachment + content + view` | 鉴权通过后，普通内容可直接使用；受保护内容从 Knowledge 服务回源原始解析 chunk |

敏感内容采用“展示文本与向量来源分离”的方式：

```text
原始文本 → Embedding → ES embedding
原始文本 → 脱敏     → ES content
原始文本 / 原始解析产物 → MinIO，由 Knowledge 服务控制回源
```

这不会让原文进入 ES `_source`。未通过内容级 `view` 检查的候选，不得返回正文、highlight、分数或进入重排模型和 LLM 上下文。只有附件元数据权限的用户，只能通过文件名等安全元数据发现附件。

### 2.1 核心字段

```text
chunk_id, chunk_index, chunk_count, chunking_version
knowledge_item_id, content_version, item_acl_version
attachment_id, attachment_content_version, attachment_acl_version
part_kind, auth_resource_type, auth_resource_id, auth_resource_part
title, content, embedding, embedding_model, embedding_source
keyword_searchable, vectorized, rag_eligible
knowledge_scope, access_scope, organization_id, owner_user_id
knowledge_base_id, conversation_ingestion_id, conversation_group_id
source_type, platform, conversation_id, conversation_name
message_id, external_message_id, sender_identity_id, sender_display_name, sent_at
file_name, mime_type, size_bytes, source_locator
content_hash, embedding_source_hash, content_visibility, sensitivity, security_policy_version
lifecycle_status, mapping_version, indexed_at, created_at
```

字段约定：

- ID、枚举、范围、版本和哈希使用 `keyword` 或数值类型。
- `title`、`content`、`conversation_name`、`sender_display_name` 和 `file_name` 使用 IK 中文分词；需要精确过滤的短文本保留 keyword 子字段。
- `content` 不建立 keyword 子字段，避免长文本触发 Lucene 单词长度限制和无意义的聚合开销。
- `embedding` 固定使用 `text-embedding-v4`、1536 维、cosine 相似度和 HNSW 索引。
- Mapping 显式使用未量化的 `hnsw`，优先保证召回质量；若后续需要降低内存，可基于真实数据评估 `int8_hnsw` 或 `bbq_hnsw`，并作为新的 `mapping_version` 重建索引，不能在 v1 中静默切换。
- `embedding_source` 只能是 `display`、`original` 或 `none`。它只描述向量来源，不能作为访问授权依据。
- `source_locator` 保存页码、段落、幻灯片、工作表、行列或字符区间，用于引用定位和鉴权后的原文 chunk 回源。
- `dynamic: strict`，索引写入前由 RAG 服务校验完整文档，禁止把平台原始 JSON 或解析器临时字段直接写入 ES。

### 2.2 稳定文档 ID

ES `_id` 与 `chunk_id` 使用同一个稳定值：

```text
sha256(
  knowledge_item_id
  + content_version
  + part_kind
  + attachment_id-or-empty
  + attachment_content_version-or-zero
  + chunking_version
  + chunk_index
)
```

重复事件和任务重试必须覆盖同一文档，而不是生成重复 chunk。一个知识条目存在多个附件时，`attachment_id` 和 `attachment_content_version` 必须进入文档 ID，避免不同附件的 `chunk_index` 冲突。

## 3. 建索引流程

### 3.1 普通消息或附件

```text
knowledge.ready
→ 校验 event_id、content_version、ACL 版本
→ 获取可展示正文或附件解析文本
→ 短文本形成一个 chunk，长文本按 token 切块
→ 使用展示文本生成 1536 维向量
→ Bulk 写入 knowledge_chunks_write
→ 校验 chunk 数量并更新 rag.index_records
```

### 3.2 敏感消息或受保护附件

```text
knowledge.ready
→ 通过内部受控接口读取原始文本和脱敏展示文本
→ 使用相同的结构边界生成一一对应的 chunk
→ 原始 chunk 只用于生成 embedding 和计算 embedding_source_hash
→ 脱敏 chunk 写入 ES content
→ 原始文本继续保存在 MinIO，不写入 ES
→ Bulk 写入后清除进程内原始文本
```

脱敏前后必须保持稳定的 chunk 对应关系。`source_locator` 指向原始解析产物中的位置，RAG 服务在用户鉴权通过后，使用 `knowledge_item_id / attachment_id + version + source_locator` 向 Knowledge 服务请求对应原始 chunk。不得把短时 URL 保存到 ES、数据库任务载荷或日志。

附件元数据文档不生成向量。只有 `vectorized=true` 且 `rag_eligible=true` 的文档才参与向量查询。

## 4. 查询、权限与 RAG

### 4.1 作用域粗筛

私人内容必须满足：

```text
knowledge_scope = private
AND owner_user_id = 当前用户
```

组织内容必须满足当前 `organization_id`。其中：

- `organization_members` 由组织范围粗筛，之后仍调用 Core 检查。
- `conversation_members` 还必须使用 `conversation_group_id` 与 Core 返回的当前有效群聊组集合做粗筛。
- ES 不保存用户成员列表；退群、退出组织、授权撤销等变化由最终 OpenFGA Check 立即生效。

`conversation_group_id` 必须保存 OpenFGA `conversation_group` 的稳定对象 ID。不能用平台原始 `conversation_id` 代替。

### 4.2 检索与鉴权顺序

```text
1. 在同一作用域过滤条件下分别执行 BM25 和向量召回。
2. 第一阶段只取 chunk ID、分数、版本、part_kind 和鉴权字段，不取 content/highlight。
3. 合并两路候选，并按 auth_resource_type/id/part 批量调用 Core/OpenFGA Check。
4. 从两路候选中删除无权限文档，重新计算允许集合内的排名。
5. 在 RAG 服务侧使用 RRF 合并允许的 BM25 和向量结果。
6. 按 knowledge_item_id 聚合；同一知识条目保留最高分部位，并保留其他命中部位供展开。
7. 仅对已授权 chunk 获取展示正文和 highlight；需要原文 RAG 时再次检查 original/content 权限并向 Knowledge 服务回源。
8. 重排模型和 LLM 只能接收第 7 步产生的已授权上下文。
```

鉴权必须早于知识条目聚合。消息正文、附件元数据和附件内容可能属于同一 `knowledge_item_id`，但它们的权限不同，不能用最高分部位的权限代表整个知识条目。

附件元数据权限不能替代附件内容权限：

- `attachment + metadata + view`：只允许展示安全元数据。
- `attachment + content + view`：允许查看内容并用于 RAG。
- `attachment + content + download`：允许下载，不因具有 `view` 自动获得。

### 4.3 BM25 与向量查询

- `title`、`content` 等字段建索引时使用 `ik_max_word`，查询时使用 `ik_smart`。
- BM25 查询只检索 `keyword_searchable=true` 的文档，并对 `title`、`file_name`、`conversation_name` 和 `content` 设置不同权重。
- 向量查询只检索 `vectorized=true AND rag_eligible=true` 且存在 `embedding` 的文档。
- BM25 和向量召回使用同一组织、用户、群聊和生命周期粗筛条件。
- RRF 在 RAG 服务侧实现，不依赖 Elasticsearch 商业许可证能力。
- 未授权候选的分数、命中数量、highlight 和正文均不得返回客户端，也不得写入搜索历史或普通日志。

### 4.4 分页

应用层 RRF 的综合分数不是 Elasticsearch 内部可直接 `search_after` 的排序值，因此不能对合并结果使用单一 ES `search_after`。

第一期使用短期服务端搜索游标：

```text
Redis cursor
├── PIT ID 和过期时间
├── BM25 search_after
├── 有界向量候选列表及当前位置
├── 已鉴权、已合并但尚未返回的结果 ID
└── 已返回的 knowledge_item_id 集合
```

首次请求打开 PIT，分别获取一批 BM25 候选和固定上限的向量候选，完成鉴权、RRF 和聚合后将剩余结果保存到 Redis。下一页先消费剩余结果；数量不足时继续推进 BM25 `search_after`，并按预设上限扩充向量候选。游标过期后要求客户端重新发起搜索，不承诺无限深的向量分页。

单路纯 BM25 搜索可以直接使用 PIT + `search_after`。单路纯向量和混合搜索均采用有界候选窗口。

## 5. 版本与生命周期

- `knowledge.ready` 的知识项版本由 `knowledge_item_id + content_version + item_acl_version` 标识。
- 附件处理还必须携带 `attachment_id + attachment_content_version + attachment_acl_version`。
- 旧内容版本不得覆盖新版本；重复 `event_id` 不得生成重复 chunk。
- 同一知识条目出现多个已索引内容版本时，查询侧只保留最高的有效 `content_version`；新版本全部写入成功并登记 ready 后，异步删除旧版本文档。
- ACL 变化只更新权限过滤元数据和版本，不重新切块或生成向量；最终权限始终以实时 OpenFGA Check 为准。
- mapping、IK 配置或向量维度不兼容时创建新物理索引，重建完成后使用单次 aliases 操作原子切换读写别名。
- 私人原件与组织共享副本保留两条独立结果，不跨归属去重。

数据库任务模型必须与多附件处理一致。可以让一个 `full_process` 任务处理知识条目的全部附件，也可以把 `attachment_id` 加入 `rag.processing_jobs` 的任务身份；不能在仅以 `knowledge_item_id + content_version + job_type` 唯一的情况下为同一消息的多个附件分别创建并发任务。

## 6. Mapping v1

使用以下请求创建首个物理索引；创建前必须确认 IK 插件可用：

```http
GET /_analyze
{
  "analyzer": "ik_smart",
  "text": "信息管理与混合检索"
}
```

```http
PUT /knowledge_chunks_v1
```

```json
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  },
  "aliases": {
    "knowledge_chunks_read": {},
    "knowledge_chunks_write": {"is_write_index": true}
  },
  "mappings": {
    "dynamic": "strict",
    "_source": {
      "excludes": ["embedding"]
    },
    "properties": {
      "chunk_id": {"type": "keyword"},
      "chunk_index": {"type": "integer"},
      "chunk_count": {"type": "integer"},
      "chunking_version": {"type": "keyword"},
      "knowledge_item_id": {"type": "keyword"},
      "content_version": {"type": "integer"},
      "item_acl_version": {"type": "long"},
      "attachment_id": {"type": "keyword"},
      "attachment_content_version": {"type": "integer"},
      "attachment_acl_version": {"type": "long"},
      "part_kind": {"type": "keyword"},
      "auth_resource_type": {"type": "keyword"},
      "auth_resource_id": {"type": "keyword"},
      "auth_resource_part": {"type": "keyword"},
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
      "embedding_model": {"type": "keyword"},
      "embedding_source": {"type": "keyword"},
      "keyword_searchable": {"type": "boolean"},
      "vectorized": {"type": "boolean"},
      "rag_eligible": {"type": "boolean"},
      "knowledge_scope": {"type": "keyword"},
      "access_scope": {"type": "keyword"},
      "organization_id": {"type": "keyword"},
      "owner_user_id": {"type": "keyword"},
      "knowledge_base_id": {"type": "keyword"},
      "conversation_ingestion_id": {"type": "keyword"},
      "conversation_group_id": {"type": "keyword"},
      "source_type": {"type": "keyword"},
      "platform": {"type": "keyword"},
      "conversation_id": {"type": "keyword"},
      "conversation_name": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}
      },
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
      "content_hash": {"type": "keyword"},
      "embedding_source_hash": {"type": "keyword"},
      "content_visibility": {"type": "keyword"},
      "sensitivity": {"type": "keyword"},
      "security_policy_version": {"type": "keyword"},
      "lifecycle_status": {"type": "keyword"},
      "mapping_version": {"type": "keyword"},
      "indexed_at": {"type": "date"},
      "created_at": {"type": "date"}
    }
  }
}
```

字段值由 RAG 服务在写入前校验：

- `embedding_model` 固定为 `text-embedding-v4`。
- `mapping_version` 固定为 `v1`。
- `attachment_metadata` 必须满足 `vectorized=false`、`rag_eligible=false`、`embedding_source=none`，并且不写 `embedding`。
- 写入 `embedding` 时必须满足 `vectorized=true`、`rag_eligible=true`，且向量长度必须为 1536。
- `content_visibility=masked` 时，`content` 和所有可展示标题、文件名、发送人名称都必须经过相应脱敏策略。
- `auth_resource_type` 使用 Contract 对外类型 `message`、`attachment`、`knowledge_item`；`auth_resource_part` 使用 `display`、`original`、`metadata`、`content`，不得直接写 OpenFGA 内部对象类型。
- `message_display` 有 `source_message_id` 时必须写 `auth_resource_type=message`；没有消息来源的派生知识对象使用 `auth_resource_type=knowledge_item`，不得由调用方任选。

## 7. 契约和数据库同步要求

Knowledge → RAG 的稳定数据或内部查询接口必须提供：

```text
knowledge_item_id, content_version, item_acl_version
attachment_id, attachment_content_version, attachment_acl_version
conversation_group_id
会话名称、发送人展示名、发送时间
附件安全展示元数据
展示正文 / 脱敏解析文本引用
原始解析产物的受控读取能力
security_policy_version
```

Core 必须提供当前用户有效的 `conversation_group_id` 集合和批量 Access Check。Knowledge 服务负责在内容回源时再次鉴权或验证由 Core 签发的短期内部授权上下文。

数据库设计落地前还需同步：

1. `rag.processing_jobs` 必须明确多附件任务策略；若一附件一任务，需要增加 `attachment_id`、`attachment_content_version`、`attachment_acl_version` 并调整未完成任务唯一约束。
2. `rag.index_records` 当前只记录知识项 ACL 版本；如需精确跟踪每个附件的索引状态，应增加附件级索引记录，或明确该表只记录整个知识条目的汇总状态。
3. `knowledge.ready` 事件必须包含上述版本和权限字段；不能只携带一个含义不明确的 `acl_version`。
4. 索引初始化程序必须先验证 ES 版本、IK analyzer 和 Embedding 维度，再创建物理索引及别名；任一检查失败时禁止启动消费任务。

## 8. 运行安全要求

- Elasticsearch 只允许 RAG 服务所在的内部网络访问，不能把 `9200` 暴露给终端用户或公网。
- 生产环境必须启用身份认证和 TLS；`xpack.security.enabled=false` 只允许用于隔离的本地开发环境。
- 搜索第一阶段显式使用 `_source` includes/excludes，只读取鉴权所需字段；鉴权通过前不请求 `content` 或 highlight。
- 原始文本生成向量后不得写入日志、异常详情、搜索历史、Trace 属性或死信事件。
- 受保护内容的查询、原文回源、RAG 使用和下载均写入安全审计。
- 第一期开启单节点且 `number_of_replicas=0`，只适合开发或可接受短暂停机的部署；正式高可用环境需增加节点和副本。
