# RAG 服务 MVP 加固与修复完整实施计划

> 状态：待审核
>
> 验收基线：`codex/rag-mvp-refactor` / `a31ced7`
>
> 计划范围：RAG 服务、Knowledge 回源契约、Core 授权契约、PostgreSQL、Elasticsearch、Redis Worker、QA History、Entity Review

## 1. 目标

本计划用于关闭 `RAG服务MVP阶段验收问题与修复清单.md` 中的 P0、P1、P2 问题，同时处理上一轮架构审查确认的高风险问题。

最终目标：

1. Phase 1A、Phase 1B 至 Phase 3 全部通过后，MVP 可以重新进入正式验收。
2. Phase 4 不参与 MVP 是否可用的判断，只处理架构债和规模问题。
3. QA History 必须写入 PostgreSQL，不允许以“未写入但接口返回成功”的形态交付。
4. 权限、原文、索引重试和任务租约必须失败关闭，不能依赖偶然正确的路径。
5. 生产代码不能重新引入旧 `memory_*` RAG 业务链路、旧表或旧 ES 索引双读双写。

## 2. 实施原则

### 2.1 先冻结契约，再修改实现

Phase 1A 先确定状态机、字段语义、接口契约和失败行为，再进入 P0/P1 实现。

### 2.2 P0/P1 优先，P2 后置

- P0 必须在重新验收前关闭。
- P1 必须在 MVP 对外发布前关闭。
- P2 必须在 MVP 前修复，或者形成明确后置记录并确认风险。

### 2.3 不做 God Repository 大拆分

Phase 1A、Phase 1B 至 Phase 3 只建立必要边界，不重写整个 Repository。Phase 4 再按 Task、Chunk、Entity、QA 四个边界逐步拆分。

### 2.4 修改点必须同时建立正确边界

被本次修改触达的 Controller、Application Service、Port 和 Repository 接口必须符合目标分层。不能为了快速修 P0/P1，把更多逻辑继续放进 Router 或单一巨大 Repository。

### 2.5 未完成能力不能伪装成成功

QA History、Entity merge、幂等审核等冻结接口在正式发布前必须完成。

开发过程中如果不对外开放对应接口，可以通过路由注册或部署流量控制实现，但不能：

- 接口正常返回成功但没有写入数据。
- 接受 `conversation_id` 却不使用。
- merge 只返回 `merged` 但不创建 Alias。
- 缺少幂等 ID 时自动生成并继续执行。

## 3. 问题来源与覆盖范围

### 3.1 验收清单问题

| 编号 | 问题 | 目标阶段 |
|---|---|---|
| P0-1 | protected 原文回源仍返回 display 内容 | Phase 2A |
| P0-2 | 公开接口信任客户端组织 Scope，Core 未验证成员关系 | Phase 2A |
| P0-3 | ES 写入失败后无法正常重试 | Phase 2B |
| P1-1 | 长任务没有续租 | Phase 3 |
| P1-2 | `owner_only` 私人任务初始 Scope 错误 | Phase 3，私人链路为 MVP 主路径时提前到 Phase 2A |
| P1-3 | AI 问答没有真正写入 QA 历史 | Phase 3 |
| P1-4 | 候选审核 `merge` 没有保存 Alias | Phase 3 |
| P1-5 | 审核幂等标识缺失时自动生成 UUID | Phase 3 |
| P2-1 | 候选实体 distinct 统计不准确 | Phase 4 |
| P2-2 | Knowledge 内存实现仍使用 `succeeded` | Phase 4 |

### 3.2 架构审查补充问题

| 编号 | 问题 | 目标阶段 |
|---|---|---|
| A-1 | 生产未配置授权 URL 时回退 `AllowAllAuthorizationGateway` | Phase 1A 和 Phase 2A |
| A-2 | 每个 HTTP 请求重新构造 Repository、连接池和授权客户端 | Phase 1A |
| A-3 | Controller 直接调用 Repository | Phase 1B 至 Phase 3 按触碰范围修复 |
| A-4 | Application Service 直接依赖具体基础设施 | Phase 1B 至 Phase 4 按触碰范围修复 |
| A-5 | Repository 过大 | Phase 4 |
| A-6 | Memory Lane 全量扫描 Chunk | Phase 4 |
| A-7 | Callback Lane 和 Branch Refresh 共用线程 | Phase 4 |
| A-8 | SearchIndexer Port 签名不完整 | Phase 1A 冻结，Phase 2B 修复 |
| A-9 | 残留探针、日志、缓存和异常目录 | Phase 4 清理，需要单独确认 |

## 4. Phase 1A：护栏、应用容器和契约冻结

Phase 1A 的目标不是关闭全部业务问题，而是保证后续修复不会继续扩大错误边界。

Phase 1 拆分为：

```text
Phase 1A：授权 fail-fast、lifespan、ApplicationContainer、状态机、跨服务契约冻结
Phase 1B：QA/EntityReview Application Service 与被触碰边界迁移
```

Phase 1B 不阻塞 Phase 2 的 P0 修复。只要 Phase 1A 的授权、容器和状态机契约冻结完成，Phase 2A 就可以开始。

### 4.1 冻结 Job 状态机

#### 4.1.1 统一状态枚举

MVP 建议统一使用：

```text
pending
processing
retry_wait
ready
metadata_only
failed
```

该枚举将替代当前实现与旧数据库设计文档不一致的状态集合。

冻结规则：

- `pending`：任务已创建，尚未被 Worker 有效领取。
- `processing`：任务已被当前 lease owner 领取并正在执行。
- `retry_wait`：发生可重试失败，等待 `next_retry_at`。
- `ready`：基础 Chunk、Embedding 和 ES 投影已经就绪，可以被检索。
- `metadata_only`：Parse 明确判定没有可索引正文，属于成功终态。
- `failed`：不可重试失败或达到最大重试次数，属于失败终态。

MVP 不把 `leased` 和 `running` 作为独立业务状态。它们由 lease 字段和 `processing` 状态共同表达。

#### 4.1.2 Stage 定义

`current_stage` 保留为内部阶段诊断：

```text
fetch
parse
chunk
index
memory
callback
```

`status` 表示任务生命周期，`current_stage` 表示当前执行阶段，两者不能混用。

#### 4.1.3 状态转换

| 当前状态 | 事件 | 下一状态 | 条件 |
|---|---|---|---|
| 无 | Dispatcher 创建任务 | `pending` | payload 合约有效 |
| `pending` | Worker claim 成功 | `processing` | lease 获取成功 |
| `retry_wait` | 到达 `next_retry_at` 且 Worker claim 成功 | `processing` | lease 获取成功 |
| `processing` | Parse 成功 | `processing` | 进入 `index` stage |
| `processing` | Index 和 ES 投影成功 | `ready` | 基础检索能力已就绪 |
| `processing` | Parse 明确判定 metadata-only | `metadata_only` | 不因 Index 故障产生 |
| `processing` | 可重试失败且未达上限 | `retry_wait` | 写入 `next_retry_at` |
| `processing` | 不可重试失败或达到上限 | `failed` | 触发失败 callback |
| `ready` | Memory Lane 成功 | `ready` | 不改变主状态 |
| `ready` | Memory Lane 失败 | `ready` | 记录 Attempt，不阻塞检索 |

#### 4.1.4 内部 Job 状态与外部 Callback 状态映射

Knowledge 只接收外部业务状态，不接收 RAG 内部调度状态。

| 内部 Job 状态 | 外部 Callback 状态 | 规则 |
|---|---|---|
| `pending` | `processing` | 任务已创建但尚未完成 |
| `processing` | `processing` | 任务正在处理 |
| `retry_wait` | `processing` | 必须带 `retryable=true` |
| `ready` | `ready` | 基础检索就绪终态 |
| `metadata_only` | `metadata_only` | Parse 明确判定的成功终态 |
| `failed` | `failed` | 不可重试或达到最大重试次数 |

禁止：

1. 把 `retry_wait` 原样发送给 Knowledge。
2. 把 RAG 内部 `current_stage` 暴露为业务状态。
3. 让 `retry_wait` 触发 Knowledge 的失败终态。

`retry_wait` 的 Callback 幂等规则：

```text
idempotency_key = source_event_id + rag_job_id + external_status
```

- 同一 Job 的 `processing` 状态最多保留一个可重放 Outbox 事件。
- 重试次数变化不得产生多个语义相同的 `processing` Callback。
- Outbox 投递失败继续重试原事件。
- `ready`、`metadata_only` 和 `failed` 各自最多产生一个终态 Callback。

### 4.2 冻结 Chunk、Embedding 和 Projection 状态

#### 4.2.1 Chunk Embedding 状态

`chunks.embedding_status` 只表示向量化状态：

```text
pending
ready
failed
```

ES 写入结果不得继续复用它表达。

#### 4.2.2 Projection 状态

完整生命周期必须冻结为：

```text
Chunk 持久化
-> 创建 projection_records(status=pending)
-> Embedding
-> ES indexing
-> ready / retry_wait / failed
```

冻结规则：

1. Projection 必须在首次 ES 写入前存在。
2. Embedding 失败不能只写 Job，必须能够在 Item/Version 维度重新查询并重试 Chunk。
3. 首次 ES 调用前，当前 Chunk 的 Projection 必须已经存在且状态为 `pending` 或 `indexing`。
4. Projection 是单个 Chunk 的 ES 投影调度权威记录。
5. Embedding 和 ES 失败共享 Projection 的重试计数，但必须记录失败阶段。

`projection_records.status` 表示 PostgreSQL Chunk 到 Elasticsearch 文档的投影状态：

```text
pending
indexing
ready
retry_wait
failed
deleted
```

状态转换：

| 当前状态 | 事件 | 下一状态 |
|---|---|---|
| `pending` | 开始写 ES | `indexing` |
| `indexing` | ES bulk 成功 | `ready` |
| `indexing` | ES bulk 临时失败且未达上限 | `retry_wait` |
| `retry_wait` | 再次开始写 ES | `indexing` |
| `indexing` | ES 持续失败并达到上限 | `failed` |
| 任意活动状态 | 新版本切换成功 | `deleted` |

规则：

- 从 `pending` 开始时可以先执行 Embedding，再进入 ES `indexing`。
- Embedding 临时失败时进入 `retry_wait`，Job 不能因为 Chunk 不在 `pending` 而误判 metadata-only。
- ES 失败时不能仅把 Chunk 的 `embedding_status` 改成 `failed`。
- Index Lane 的重试查询必须包含允许重试的 `projection_records`。
- ES 文档必须继续使用确定性 `_id=chunk_id`。
- 重复执行同一投影必须幂等。

`projection_records` 增加独立字段：

```text
retry_count
next_retry_at
last_error
failure_stage
```

`failure_stage` 建议取值：

```text
embedding
indexing
```

唯一键冻结为：

```text
chunk_id + es_index_alias + mapping_version
```

说明：

- MVP 的 display/protected 读写别名固定，索引别名可以作为 Projection 身份的一部分。
- 如果未来执行别名迁移或 alias 扩缩容，必须把映射版本纳入迁移，不能直接复用旧记录。

版本切换规则：

1. 当前 `knowledge_item_id + content_version` 的所有有效 Projection 达到 `ready` 前，不能删除上一版本投影。
2. 当前版本失败时，上一版本可以继续服务已知用户，直到重建或人工处理。
3. 当前版本全部 `ready` 后，才能把上一版本 Projection 标记为 `deleted` 并清理 ES 文档。

### 4.3 冻结 metadata-only 判定

`metadata_only` 只能由 Parse 结果明确产生，不能在 Index Lane 中因为“查不到 pending Chunk”而推断。

`parse_status` 必须记录在 Job 上，不能只放在 Snapshot。Knowledge 拉取、附件下载或解析器在 Snapshot 创建前失败时，Snapshot 可能不存在。

建议增加 `processing_jobs.parse_status`：

```text
pending
parsed
metadata_only
failed
```

冻结规则：

1. Job 创建后默认为 `pending`。
2. Parse 成功且存在 Chunk，`parse_status=parsed`。
3. Parse 确认无正文但业务处理成功，`parse_status=metadata_only`。
4. Knowledge 拉取、附件下载或解析器早期失败，`parse_status=failed`。
5. Index Lane 只有在 `parse_status=metadata_only` 时才能返回 `metadata_only`。
6. 如果存在 Chunk 或历史投影，但当前 Embedding/ES 写入失败，Job 必须进入 retry 或 failed，不能进入 `metadata_only`。
7. 历史数据迁移不能统一回填为 `parsed`，必须根据同 Item/Version 是否存在 Chunk 回填：

```text
存在 Chunk -> parsed
不存在 Chunk 且已成功终态 -> metadata_only
其余 -> pending 或 failed，按实际任务状态决定
```

### 4.4 冻结 Retry 和终态规则

冻结字段：

```text
retry_count
next_retry_at
last_error
max_retries
retryable
```

建议规则：

- `processing_jobs.retry_count` 是 Job 级累计重试次数。
- `processing_job_attempts` 保留每个 Lane、Stage 和 lease epoch 的执行记录。
- 每次可重试失败都递增 `retry_count` 并写入 `retry_wait`。
- 达到 `task_max_retries` 后必须进入 `failed`。
- `failed` 和 `ready` 是不可回退终态。
- 只有显式 `reindex` 或新 `content_version` 可以创建新任务。
- Callback 投递失败不能改变 Job 的 `ready` 或 `failed` 结果，只重试 Outbox。

#### 4.4.1 Job Retry 与 Projection Retry 的职责

冻结职责：

```text
Projection Retry：单个 Chunk 的 Embedding 或 ES 投影失败
Job Retry：整个 Index Stage 当前无法继续调度
```

调度规则：

1. Projection 负责记录 Chunk 级 `retry_count`、`next_retry_at` 和 `last_error`。
2. Job 不重复维护 Chunk 级失败列表。
3. 当前版本存在任意 `pending`、`indexing` 或 `retry_wait` Projection 时，Job 可以进入 `retry_wait`。
4. 所有当前版本 Projection 达到终态后，Job 才能进入 `ready` 或 `failed`。
5. 任意 Projection 为 `failed` 且未达到恢复上限时，Job 不能提前 `ready`。
6. 所有 Projection 为 `ready` 时，Job 进入 `ready`。
7. 存在不可恢复 Projection 或达到 Job/Projection 上限时，Job 进入 `failed`。

这样 Index Stage 只负责“是否还能继续调度”，Projection 负责“具体哪个 Chunk 需要重试”，避免两套重试调度竞争。

### 4.5 冻结 Lease 和 Fencing 契约

当前只有 `lease_owner` 和 `lease_until`，不足以保证旧 Worker 停止写入。

建议增加：

```text
processing_jobs.lease_epoch BIGINT NOT NULL DEFAULT 0
```

每次成功 claim 时：

```text
lease_owner = <worker identity>
lease_epoch = lease_epoch + 1
lease_until = now() + lease_seconds
```

所有后续写入必须满足：

```text
WHERE id = job_id
  AND lease_owner = current_owner
  AND lease_epoch = current_epoch
```

需要 CAS 的接口：

```text
heartbeat(job_id, owner, epoch) -> bool
update_job_if_owned(job_id, owner, epoch, fields) -> bool
complete_job_if_owned(job_id, owner, epoch, result) -> bool
fail_job_if_owned(job_id, owner, epoch, error) -> bool
```

冻结行为：

1. Heartbeat 失败时，Worker 必须立即停止当前任务。
2. 完成、失败、Attempt、Outbox 和 Callback 写入都不能绕过 fencing 检查。
3. 旧 Worker 失去 lease 后，即使业务操作已经完成，也不能覆盖新 Worker 的状态。
4. 进程崩溃后，新 Worker 可在 lease 过期后递增 epoch 并恢复任务。
5. Redis 重复投递仍由 Job 幂等和 lease 双重保护。

实现可以放 Phase 3，但接口和字段必须在 Phase 1A 冻结。

### 4.6 冻结 Knowledge 回源契约

#### 4.6.1 内容变体

```text
消息 display  -> messages.normalized_content
消息 original -> message_private_content
附件内容      -> GetAttachmentForRAG + ObjectStore
```

规则：

- 消息 `original` 不允许静默回退到 display。
- `original_content_ref` 可能是对象引用，不能直接作为文本返回。
- 附件内容通过 `GetAttachmentForRAG` 获取内部 object_ref，再由 ObjectStore 读取。
- 原文或附件对象不可用时返回明确的不可用错误。
- `purpose=index` 仍只允许 RAG 服务身份读取 protected 原文。
- 普通用户访问 protected 原文继续执行审批和授权。

#### 4.6.2 Audit 字段

Knowledge 回源审计至少记录：

```text
caller_service
purpose
knowledge_item_id
resource_id
content_version
acl_version
content_variant
rag_job_id
trace_id
result
created_at
```

RAG 请求建议通过 Header 传递：

```text
X-RAG-Job-ID: <job uuid>
X-Trace-ID: <trace id>
```

审计写入失败时的行为必须按内容类型冻结：

- protected 原文：失败关闭。
- protected 附件内容：失败关闭。
- display 内容：按验收要求决定失败关闭或记录可靠补偿事件。

推荐 MVP 对 protected 原文和附件内容采用失败关闭。审计写失败时，不得返回 protected 正文或附件对象引用。

### 4.7 冻结 `knowledge.ready` 权威 Scope

payload 增加：

```text
scope_type
scope_id
owner_user_id
organization_id
knowledge_scope
```

唯一权威字段只有：

```text
scope_type
scope_id
```

其余字段只用于：

1. 元数据展示。
2. 一致性校验。
3. 排障和审计。

`owner_user_id`、`organization_id`、`knowledge_scope` 不得覆盖或优先于 `scope_type/scope_id`。

兼容规则：

1. Knowledge 先新增字段，RAG 接受新版 payload。
2. 过渡期允许 RAG 读取旧字段，但不能对 `owner_only` 回退到零 UUID。
3. `owner_only` 缺少 `owner_user_id` 且无法验证 `scope_type=user` 时拒绝创建任务。
4. 组织内容缺少 `organization_id` 且无法验证 `scope_type=organization` 时拒绝创建任务。
5. Parse 回源的 Scope 与事件 Scope 不一致时失败。

建议最终权威规则：

```text
source_audience_policy=owner_only
-> scope_type=user
-> scope_id=owner_user_id

source_audience_policy=organization_members
-> scope_type=organization
-> scope_id=organization_id

source_audience_policy=source_conversation_members
-> scope_type=organization
-> scope_id=organization_id
-> 会话权限由 Core/ES 授权字段继续约束
```

### 4.8 冻结授权和 `truncated` 行为

冻结规则：

1. 生产环境缺少 `RAG_AUTHZ_BASE_URL` 时启动失败。
2. `AllowAllAuthorizationGateway` 只允许 development/test，且启动日志必须明确警告。
3. Core 必须验证用户与请求组织的关系。
4. 用户无组织、组织不可确认或成员关系不可确认时失败关闭。
5. `ListObjects` 必须受组织上下文约束。
6. `truncated=true` 时不能返回可能不完整的授权结果。

推荐行为：

```text
snapshot_truncated=true
-> RAG 返回 503 authz_unavailable
```

如果未来只允许 protected 列表截断，必须引入独立字段，例如：

```text
protected_scope_truncated=true
-> 禁止 protected
-> display 是否可用取决于其授权集合是否完整
```

### 4.9 冻结 QA History 契约

QA History 是正式功能，不是可默认关闭的可选功能。

普通接口：

```text
POST /api/v1/ai/documents
```

必须执行：

1. 无 `conversation_id` 时创建会话。
2. 有 `conversation_id` 时校验用户和 Scope。
3. 写 user message。
4. 执行检索。
5. 写 assistant message。
6. 返回 `conversation_id`、`user_message_id`、`assistant_message_id`。

流式接口：

```text
POST /api/v1/ai/documents/stream
```

必须执行：

1. 写 user message。
2. 创建 `assistant.status=streaming` 消息。
3. SSE `meta` 返回 conversation 和 message ID。
4. `done` 后更新为 `completed`。
5. `error` 或连接中断后更新为 `failed`。
6. 保存安全错误信息、citations、diagnostics 和 prompt/model 版本。

不允许：

- 无数据库写入却返回成功。
- 无效 conversation ID 被忽略。
- 流式失败后没有 assistant 失败记录。
- 用配置默认关闭 QA History 来通过验收。

### 4.10 冻结 Entity Review 契约

#### 4.10.1 merge

`merge` 必须在同一事务内完成：

1. 锁定候选。
2. 校验目标 Entity 属于当前 `scope_type/scope_id`。
3. 从候选规范名创建或更新 Alias。
4. 更新 `resolved_entity_id`。
5. 写 review request 结果。
6. 创建 Branch Refresh Job。

#### 4.10.2 幂等

必须规则：

- `review_request_id` 和 `Idempotency-Key` 至少提供一个。
- 两者同时提供但值不同时返回 422。
- 缺少幂等 ID 时不得自动生成。
- 同一 `candidate_id + review_request_id` 只能执行一次。
- 重放返回第一次的完整结果。

### 4.11 应用容器和依赖生命周期

新增统一容器：

```text
ApplicationContainer
  repository
  indexer
  embedding
  authorization
  qa_service
  entity_review_service
  callback_service
```

FastAPI 使用 lifespan：

1. 启动时创建容器。
2. 将容器放入 `app.state`。
3. Router 通过依赖获取 Application Service。
4. 关闭时释放 PostgreSQL 连接池和其它资源。

约束：

- 不允许每个 HTTP 请求调用 `build_repository()`。
- 不允许每个 HTTP 请求重新创建连接池或授权客户端。
- `bootstrap.py` 是唯一组合根，业务服务不直接创建基础设施对象。
- 被本次修改触达的 Controller 不得直接访问 Repository。
- Worker 使用相同组合根参数，但可以在独立进程中创建自己的容器。

### 4.12 Ports 契约冻结

Phase 1A 冻结以下 Ports 签名。接口可以在 Phase 2 或 Phase 3 完成实现，但不能继续依赖具体实现类。

```text
SearchIndexer.create_indices(recreate: bool = False)
SearchIndexer.search_bm25(..., protected_object_keys, size)
SearchIndexer.search_knn(..., protected_object_keys, size)
TaskRepository.claim_jobs(..., lease_seconds)
TaskRepository.heartbeat(job_id, owner, epoch)
TaskRepository.update_if_owned(job_id, owner, epoch, fields)
TaskRepository.complete_if_owned(job_id, owner, epoch, result)
TaskRepository.fail_if_owned(job_id, owner, epoch, error)
```

Application Service 只能依赖 Ports 和领域模型，不直接 import PostgreSQL、Elasticsearch、Redis 或 Knowledge HTTP 实现。

### 4.13 Phase 1A 数据迁移

建议迁移：

1. `processing_jobs.lease_epoch BIGINT NOT NULL DEFAULT 0`。
2. `processing_jobs.parse_status VARCHAR(16) NOT NULL DEFAULT 'pending'`。
3. `projection_records.status` 增加 `retry_wait`。
4. `projection_records.retry_count INTEGER NOT NULL DEFAULT 0`。
5. `projection_records.next_retry_at TIMESTAMPTZ`。
6. `projection_records.last_error TEXT`。
7. `projection_records.failure_stage VARCHAR(16)`。
8. 将 `projection_records` 唯一键调整为 `chunk_id + es_index_alias + mapping_version`。
9. 为 `processing_jobs(status, next_retry_at, lease_until)` 准备 CAS 调度索引。
10. 为 `projection_records(status, es_index_alias, next_retry_at)` 准备重试索引。
11. 新增 Knowledge 内容回源审计表。
12. 如 QA 表缺少更新 assistant 状态所需字段，补齐状态、错误和诊断字段。

所有迁移必须提供 down migration，采用新增列和新增索引，不直接删除旧列。

历史数据回填：

1. `processing_jobs.parse_status` 不能统一默认成 `parsed`。
2. 已存在同 Item/Version Chunk 的任务回填 `parsed`。
3. 无 Chunk 且已经成功结束的任务回填 `metadata_only`。
4. 其它任务保留 `pending` 或按真实失败记录回填 `failed`。
5. Projection 历史数据按现有投影状态回填，并补齐 `retry_count=0`。

### 4.14 Phase 1A 提交切分

```text
commit 1: docs(rag): freeze hardening contracts and phase plan
commit 2: feat(rag): add canonical job and projection state contracts
commit 3: feat(rag): add lease epoch and owned mutation ports
commit 4: refactor(rag): add application container and lifespan
commit 5: test(rag): cover contracts, fail-fast settings and owned writes
```

### 4.15 Phase 1A 退出条件

- 状态机和失败语义文档已冻结。
- 生产漏配授权时启动失败。
- 生产不再使用 AllowAll。
- 应用容器启动和关闭正常。
- 被触碰接口不再直接依赖 Repository。
- QA、merge、幂等和权威 Scope 的目标契约已写入文档。
- Phase 2、Phase 3 的测试矩阵已建立。

## 5. Phase 1B：Application Service 和被触碰边界迁移

Phase 1B 不阻塞 Phase 2A。它主要防止 QA、Entity Review 和 Controller 继续扩大错误分层。

### 5.1 新增 Application Service

至少建立：

```text
QAService
EntityReviewService
RetrievalService
AuthorizationScopeService
TaskApplicationService
```

约束：

1. Application Service 只依赖 Ports 和领域模型。
2. Controller 不直接调用 Repository。
3. QA 和 Entity Review 的完整持久化可以在 Phase 3 完成，但接口边界必须在 Phase 1B 固定。
4. 如果 Phase 1B 尚未完成持久化实现，内部开发版本不得对生产开放对应外部流量。

### 5.2 被触碰 Controller 迁移

优先迁移：

```text
routers/api.py
routers/search.py
routers/admin.py
```

目标：

```text
Controller
-> Application Service
-> Port
-> Adapter
```

### 5.3 Phase 1B 提交切分

```text
commit 1: refactor(rag): add qa application service boundary
commit 2: refactor(rag): add entity review application service boundary
commit 3: refactor(rag): route touched controllers through application services
commit 4: test(rag): cover application service and controller boundaries
```

### 5.4 Phase 1B 退出条件

- QA 和 Entity Review 不再由 Router 直接操作 Repository。
- 被本次修改触达的接口全部通过 Application Service。
- Ports 签名与实际调用一致。
- 未完成的持久化能力不会对生产返回伪成功。

## 6. Phase 2A：安全与数据正确性

Phase 2A 的两个安全闭环可以并行推进，但必须分别提交和验收。

### 6.1 P0-1 protected 原文和审计

#### 6.1.1 实现

1. `GetKnowledgeContent` 接收并使用 `content_variant`。
2. display 从 `messages.normalized_content` 读取。
3. 消息 original 只从 `message_private_content` 读取。
4. 附件 original 由 `GetAttachmentForRAG` 返回内部 object_ref，再由 ObjectStore 读取。
5. `original_content_ref` 只作为对象引用处理，不直接当作文本返回。
6. original 或附件对象不存在时返回明确错误，不回退 display。
7. RAG 调用时传递 `X-RAG-Job-ID` 和 trace。
8. Knowledge 写入内容回源审计。

#### 6.1.2 测试

```text
test_display_content_returns_normalized_text
test_original_content_returns_private_text
test_original_missing_does_not_fallback_to_display
test_protected_es_document_equals_original_text
test_display_es_document_equals_redacted_text
test_rag_source_audit_contains_purpose_and_job_id
```

### 6.2 P0-2 Core 组织成员验证和 Scope

#### 6.2.1 Phase 2A 目标

允许 RAG 暂时继续接收组织参数，但 Core 必须校验：

```text
user_id 是否属于 organization_id
```

实现方式：

1. Core 复用或扩展当前 internal member check。
2. `search-scope` 在返回授权组织前执行成员校验。
3. `check-batch` 也使用相同组织上下文。
4. `ListObjects` 返回结果必须受组织约束。
5. OpenFGA 不支持组织上下文时，在 Core 中二次过滤或批量 Check。

#### 6.2.2 唯一组织接口

如果产品确认用户只有一个有效组织，可增加：

```text
POST /internal/v1/organizations/resolve-current
```

返回：

```json
{
  "available": true,
  "organization_id": "uuid"
}
```

无组织、多组织或成员关系不确定时：

```json
{
  "available": false,
  "reason": "membership_ambiguous"
}
```

如果未来可能支持多组织，应优先提供 membership list 或 active organization，而不是把“唯一组织”永久写入公共契约。

#### 6.2.3 truncated

1. Core 计算真实截断状态。
2. `truncated=true` 时 RAG 不返回部分授权结果。
3. 推荐返回 `503 authz_unavailable`。
4. 前端不能缓存失败关闭结果。

#### 6.2.4 测试

```text
test_search_scope_rejects_non_member_organization
test_check_batch_uses_verified_organization
test_list_objects_is_limited_to_organization
test_truncated_authorization_fails_closed
test_user_without_organization_cannot_search_organization
test_multi_organization_returns_explicit_ambiguity
```

### 6.3 Phase 2A 提交切分

```text
commit 1: fix(knowledge): return canonical original content and write audit
commit 2: fix(core): verify organization membership for authorization scopes
commit 3: test(core-rag): cover cross-tenant scope rejection and truncation
```

## 7. Phase 2B：索引可靠性

### 7.1 P0-3 ES 重试

实现：

1. Index Service 从 `projection_records` 查询 `pending` 和可重试的 `retry_wait`。
2. ES 临时失败写入 `retry_wait`、`last_error`、`next_retry_at`。
3. 达到上限后写 `failed`。
4. Job 保持 `retry_wait` 或最终 `failed`。
5. `metadata_only` 只读取 Phase 1A 冻结的 Parse 标记。
6. 删除旧版本前必须确保当前版本投影可重试，不能因失败永久丢失可恢复版本。

### 7.2 测试

```text
test_es_failure_enters_retry_wait
test_es_retry_succeeds_and_job_becomes_ready
test_es_retry_exhaustion_fails_job
test_es_failure_never_returns_metadata_only
test_parse_metadata_only_remains_success
test_repeated_projection_uses_same_es_document_id
test_index_retry_does_not_duplicate_documents
```

### 7.3 Phase 2B 提交切分

```text
commit 1: fix(rag): retry failed elasticsearch projections
commit 2: test(rag): inject elasticsearch failures and verify terminal states
```

## 8. Phase 3：P1 可靠性和业务契约闭环

### 8.1 P1-1 Heartbeat 和 Fencing

实现：

1. Worker 领取任务时保存 owner 和 epoch。
2. Parse、Index、Memory Lane 运行时启动 heartbeat 线程。
3. Heartbeat 间隔使用 `RAG_TASK_HEARTBEAT_SECONDS`。
4. 所有 Job 状态写入使用 owner + epoch CAS。
5. Heartbeat CAS 失败时停止当前 Worker 后续写入。
6. 完成、失败、Outbox 和 Attempt 都受 fencing 控制。

测试：

```text
test_long_running_job_cannot_be_claimed_by_second_worker
test_stale_worker_heartbeat_is_rejected
test_stale_worker_cannot_complete_job
test_crashed_worker_job_is_recovered_after_lease_expiry
test_new_worker_epoch_blocks_old_worker_writes
```

### 8.2 P1-2 权威 Scope

实现：

1. Knowledge `knowledge.ready` 增加权威 Scope 字段。
2. RAG Dispatcher 优先使用事件权威 Scope。
3. owner-only 内容缺少 owner 时拒绝。
4. 组织内容缺少 organization 时拒绝。
5. Parse 回源 Scope 不一致时失败。
6. Phase 3 移除客户端组织作为权威来源。

RAG 对外接口最终只接受：

```text
X-User-ID
```

组织由 Core 根据用户活动上下文或成员关系解析。

### 8.3 P1-3 QA History 持久化

新增 `QAService`，Router 不直接访问 Repository。

普通问答流程：

```text
resolve conversation
-> create user message
-> search and answer
-> create assistant message
-> return message ids
```

流式问答流程：

```text
resolve conversation
-> create user message
-> create assistant streaming message
-> stream tokens
-> complete or fail assistant message
```

Repository 需要新增：

```text
get_qa_conversation_for_scope
update_qa_message_status
append_qa_message
```

测试：

```text
test_qa_creates_conversation_and_two_messages
test_qa_reuses_owned_conversation
test_qa_rejects_other_user_conversation
test_stream_creates_pending_assistant_message
test_stream_done_marks_assistant_completed
test_stream_error_marks_assistant_failed
test_qa_response_contains_all_ids
```

### 8.4 P1-4 Entity merge Alias

实现：

1. merge 改为 Application Service 用例。
2. 候选和目标 Entity 在相同 Scope。
3. 同一事务写 Alias、候选状态、review request 和 Branch Refresh Job。
4. 幂等重放不重复创建 Alias。

测试：

```text
test_merge_creates_alias
test_merged_alias_matches_future_chunks
test_merge_rejects_cross_scope_target
test_merge_replay_does_not_duplicate_alias
test_merge_alias_failure_rolls_back
```

### 8.5 P1-5 审核幂等

实现：

1. `review_request_id` 和 `Idempotency-Key` 至少一个。
2. 两者冲突返回 422。
3. 缺失时禁止生成 UUID。
4. 重复请求返回第一次完整结果。
5. 不同请求 ID 对终态候选执行时按状态冲突处理。

测试：

```text
test_review_requires_idempotency_key
test_review_rejects_conflicting_idempotency_keys
test_review_replay_returns_same_result
test_review_replay_does_not_create_duplicate_refresh_job
test_terminal_candidate_rejects_new_request
```

### 8.6 Phase 3 提交切分

```text
commit 1: feat(rag): enforce heartbeat fencing for job mutations
commit 2: feat(rag): consume authoritative knowledge scope
commit 3: feat(rag): persist qa conversations and messages
commit 4: fix(rag): create aliases during entity merge
commit 5: fix(rag): require explicit review idempotency
commit 6: test(rag): add cross-service phase three acceptance coverage
```

## 9. Phase 4：P2 和架构债

Phase 4 不参与 MVP 是否可用的判断。Phase 1A、Phase 1B 至 Phase 3 集成测试通过后，才进入该阶段。

### 9.1 P2-1 候选统计

实现：

1. 从 `entity_candidate_mentions` 聚合 distinct 统计。
2. 统计维度：

```text
COUNT(DISTINCT chunk_id)
COUNT(DISTINCT source_resource_id)
COUNT(DISTINCT source_conversation_id)
```

3. 如果 Mention 表缺少来源维度，先补字段并回填。
4. 列表、详情和评分使用同一统计来源。

### 9.2 P2-2 Knowledge 内存状态

1. `RAGStatus == "succeeded"` 改为 `ready`。
2. 明确 `processing`、`ready`、`metadata_only`、`failed` 的 VectorStatus 映射。
3. 增加内存和 PostgreSQL 行为一致性测试。

### 9.3 Memory Lane 优化

1. Repository 增加按 `knowledge_item_id + content_version` 和 Scope 查询 Chunk 的方法。
2. Memory Service 不再加载全部 ready Chunk。
3. 增加数据量测试，验证查询量与目标资源相关，而不是与全库规模相关。

### 9.4 Callback 和 Branch Refresh 解耦

1. Callback Flush 和 Branch Refresh 使用独立线程或调度器。
2. Branch Refresh 延迟不得阻塞状态回调。
3. 两个 Lane 分别增加队列深度、耗时和失败指标。

### 9.5 Repository 拆分

拆分顺序：

```text
TaskRepository
ChunkRepository
EntityRepository
QAHistoryRepository
```

约束：

1. 不一次性重写全部 Repository。
2. 每次只移动一个聚合。
3. 旧方法先委托新实现，再逐步删除。
4. 拆分不得改变公开 API 和 Callback 契约。

### 9.6 残留文件清理

单独确认后处理：

- `_probe_*.py`
- `worker-runtime.log`
- `__pycache__`
- 异常 `%SystemDrive%` 目录
- 旧 `memory_*` 表和索引的保留策略

不得在首次实施中无确认地删除历史数据或旧索引。

## 10. 测试和验收矩阵

### 10.1 基础测试

```powershell
cd services/rag
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider

cd services/knowledge
go test ./...

cd services/core
go test ./...
```

### 10.2 RAG 真实集成测试

```powershell
cd services/rag
$env:RAG_MVP_INTEGRATION = "1"
.\.venv\Scripts\python.exe -B -m pytest tests/test_mvp_integration.py -q -p no:cacheprovider
```

### 10.3 跨服务集成测试

```powershell
cd services/rag
$env:RAG_CROSS_SERVICE_INTEGRATION = "1"
.\.venv\Scripts\python.exe -B -m pytest tests/test_cross_service_integration.py -q -p no:cacheprovider
```

真实集成测试必须使用：

```text
PostgreSQL rag_mvp
Elasticsearch rag_chunks_display/protected
Redis Streams
Knowledge HTTP 回源
Core Authorization API
```

### 10.4 Phase 1A 和 P0 专项验收

1. protected 原文与 display 不同时，protected ES 文档必须等于原文。
2. 修改 body/header 指向其他组织时，搜索必须失败关闭。
3. ES 第一次失败、第二次成功时，Job 最终为 `ready`。
4. ES 持续失败并达到上限时，Job 最终为 `failed`，不能是 `metadata_only`。
5. 长任务超过一个 lease 周期时，不能被其他 Worker 同时执行。
6. 生产漏配授权时应用启动失败。
7. 多次请求复用同一个 Repository、授权客户端和连接池。
8. Projection 在首次 ES 调用前已经存在。
9. Embedding 失败后可以按 Item/Version 重新查询并重试。
10. `retry_wait` 不原样暴露，只映射为外部 `processing + retryable=true`。
11. protected 审计写失败时失败关闭。
12. 新版本全部 Projection `ready` 前，不删除上一版本投影。
13. `knowledge.ready` 新旧字段兼容。
14. Parse 早期失败时 `parse_status=failed`。
15. 迁移 upgrade、down 和历史数据回填通过。
16. 非法 Job 和 Projection 状态转换被拒绝。

### 10.5 P1 专项验收

1. 私人任务从 Dispatcher 开始使用正确 owner Scope。
2. QA 普通和流式接口都持久化历史并返回消息 ID。
3. merge 后 Alias 可用于正式 Entity 匹配。
4. 缺少审核幂等 ID 时返回 422，不修改数据。
5. Heartbeat 失租后旧 Worker 不能继续写终态。
6. `retry_wait` 只映射外部 `processing + retryable=true`，不暴露内部状态。
7. 相同 Job 和外部状态的 Callback 事件保持幂等。
8. 消息 original 从 private content 读取，附件 original 通过 ObjectStore 读取。
9. `original_content_ref` 不会被直接当作文本返回。
10. QA 流式异常后存在 failed assistant message。

### 10.6 Projection 和 Job 重试职责验收

1. Projection `retry_wait` 存在时，Job 不会提前进入 `ready`。
2. 所有当前版本 Projection `ready` 后，Job 才进入 `ready`。
3. 达到 Projection 上限后，Job 最终进入 `failed`。
4. Job Retry 负责 Index Stage 调度，不重复维护 Chunk 失败列表。
5. 任意 Projection 非法跨越状态时返回失败并保留原状态。

### 10.7 P2 验收

1. 同一 Chunk 同一候选重复出现不增加 distinct Chunk。
2. 同一会话多个 Chunk 正确更新 Chunk 数，不错误增加 conversation 数。
3. Knowledge 内存和 PostgreSQL 的 VectorStatus 一致。
4. Memory Lane 不扫描全量 ready Chunk。
5. Callback 不再被 Branch Refresh 阻塞。

## 11. MVP 重新验收通过条件

必须同时满足：

1. 验收清单 P0 全部关闭。
2. 验收清单 P1 全部关闭。
3. P2 已修复，或形成明确后置项并获得确认。
4. RAG 单元测试全部通过。
5. Knowledge 和 Core Go 测试全部通过。
6. RAG MVP 集成测试和跨服务集成测试真实启用并通过。
7. 生产授权配置缺失时启动失败。
8. 没有使用 AllowAll 处理生产请求。
9. 没有 QA 接口未落库但返回成功的情况。
10. 没有 old `memory_*` RAG 业务链路。
11. 没有旧表和旧 ES 索引双读或双写。

## 12. 分支、提交和回滚策略

### 12.1 分支

建议从验收基线创建独立分支：

```text
codex/rag-mvp-hardening
```

如果沿用 `codex/rag-mvp-refactor`，必须先确认工作区只包含本阶段预期修改。

### 12.2 提交

1. 每个 Phase 独立提交。
2. 每个 P0 独立提交，不把多个安全问题混在一个提交。
3. 迁移和对应 Repository 修改放在同一提交。
4. 测试可以和修复同提交，跨服务契约测试可以单独提交。

### 12.3 回滚

1. 所有迁移先使用新增字段和索引，避免不可逆删除。
2. 代码回滚后旧字段仍然存在，保证数据库兼容。
3. `knowledge.ready` Scope 字段采用 producer-first 兼容发布。
4. Core 成员校验从首个对外版本开始默认 fail-closed。只允许在非生产环境做 shadow 统计。生产环境不得以观察期为由放行非成员请求。
5. QA 和 merge 属于正式接口修复，不提供“返回成功但不落库”的回滚模式。

## 13. 部署顺序

### 13.1 Scope 字段兼容发布

```text
1. Knowledge 增加权威 Scope 字段
2. RAG 增加新旧字段兼容读取
3. 验证 owner_only 和 organization 两类事件
4. RAG 改为必须使用权威 Scope
5. 移除旧回退
```

### 13.2 授权严格化

```text
1. 非生产环境先支持成员校验和 shadow 统计
2. Core 对首个对外版本默认 fail-closed
3. RAG 调用 Core 校验并拒绝非成员
4. 开启 truncated 失败关闭
5. 移除客户端权威 organization
```

### 13.3 QA History

```text
1. Application Service 和 Repository 方法先合并
2. 数据库写入测试通过
3. 普通接口发布
4. 流式接口发布
5. 前端切换到返回的消息 ID
6. 不允许在未落库版本上开放外部流量
```

## 14. 待审核决策

以下事项建议在实施前确认：

1. MVP 用户是否确定为单一有效组织。
2. `metadata_only` 是否按本计划保留为 `processing_jobs.status` 的终态。
3. Knowledge 内容回源审计是否使用新增的 Knowledge 自有审计表。
4. `truncated=true` 是否统一返回 `503 authz_unavailable`。
5. owner-only 私人知识库是否属于 MVP 主演示路径。
6. Phase 4 后置项是否需要单独创建 GitHub Issue 或后续 Milestone。

## 15. 最终阶段口径

```text
Phase 1A：授权 fail-fast、lifespan、ApplicationContainer、状态机、契约冻结
Phase 1B：QA/EntityReview Application Service、被触碰边界迁移
Phase 2A：protected 原文、审计、Core 组织成员验证
Phase 2B：ES 状态机和失败重试
Phase 3：Heartbeat、权威 Scope、QA History、merge Alias、幂等
Phase 4：P2、性能优化和 Repository 架构债
```

核心原则：

1. Phase 1A 可以少写实现，但必须冻结接口、状态和失败语义。
2. Phase 2 优先关闭数据正确性和权限风险。
3. Phase 3 完成 P1 和用户可见业务闭环。
4. Phase 4 不参与 MVP 是否可用的判断。
5. QA History 是正式持久化能力，不存在默认关闭的正式交付形态。
