# RAG 服务 MVP 阶段验收问题与修复清单

> 状态：待修复后重新验收  
> 验收基线：`codex/rag-mvp-refactor` / `a31ced7`  
> 适用范围：Phase 0 至 Phase 7 主链路、服务三 RAG、服务二 Knowledge 最小契约、服务一 Core 授权契约  
> 结论：结构改造和主链路已经落地，但当前不能判定 Phase 7 验收通过

## 1. 验收结论

当前版本已经完成 `rag_mvp`、新 ES 索引别名、Dispatcher/Parse/Index/Memory/Callback
多 Lane、确定性 Chunk、受控 Entity Registry、候选审核和组织树接口等主体改造。

但以下问题会直接影响以下冻结目标：

- protected 原文必须可索引，display 只能作为脱敏展示版本。
- 客户端不能指定权威组织 Scope，不能跨组织检索。
- 失败任务必须可恢复、可重试，不能把索引失败误报为 `metadata_only`。
- 长任务必须保持租约，不能因 MinerU 等慢处理被重复领取。
- 冻结的 QA、审核和候选接口必须真正落库并具备幂等性。

因此当前状态定义为：

```text
结构完成
主链路可运行
Phase 7 验收未通过
不得以当前版本作为 MVP 最终验收版本
```

## 2. 优先级定义

| 优先级 | 定义 | 处理要求 |
|---|---|---|
| P0 | 权限、原文、数据正确性或恢复能力存在阻断问题 | 必须修复后才能重新验收 |
| P1 | 冻结接口或关键运行能力未实现或不可靠 | MVP 验收前必须修复 |
| P2 | 统计、内存实现和一致性问题 | MVP 验收前修复，或形成明确的后置项并记录风险 |

## 3. 问题总表

| ID | 优先级 | 问题 | 主要影响 |
|---|---|---|---|
| P0-1 | P0 | protected 原文回源仍返回 display 内容 | protected 索引可能没有真正原文 |
| P0-2 | P0 | 公开接口信任客户端组织 Scope，Core 未验证成员关系 | 跨租户隔离不可验收 |
| P0-3 | P0 | ES 写入失败后无法正常重试 | 索引失败可能被误报为 `metadata_only` |
| P1-1 | P1 | 长任务没有续租 | 慢任务可能被重复领取执行 |
| P1-2 | P1 | owner_only 私人任务初始 Scope 错误 | 初始回调、失败任务和审计 Scope 不可信 |
| P1-3 | P1 | AI 问答没有真正写入 QA 历史 | 冻结接口不成立 |
| P1-4 | P1 | 候选审核 `merge` 没有保存 Alias | 合并后的候选名称不能被正式实体识别 |
| P1-5 | P1 | 审核幂等标识缺失时自动生成 UUID | 超时重试可能重复执行审核 |
| P2-1 | P2 | 候选实体 distinct 统计不准确 | 管理端统计与验收数据失真 |
| P2-2 | P2 | Knowledge 内存实现仍使用 `succeeded` | 内存测试语义与生产 `ready` 不一致 |

## 4. P0 问题与修复

### P0-1 protected 原文回源仍返回 display 内容

**问题**

Knowledge 的 PostgreSQL 回源实现始终读取 `messages.normalized_content`，并将
`content_variant` 固定为 `display`：

```text
services/knowledge/internal/repository/postgres.go:2520
```

服务层在请求 `original` 时只修改返回值中的 variant，没有切换内容来源：

```text
services/knowledge/internal/service/service.go:2587
services/knowledge/internal/service/service.go:2602
```

敏感消息完成隐私分类后，`normalized_content` 保存 display 内容，原文保留在
`message_private_content`。因此 RAG 以 `content_variant=original&purpose=index`
回源时，仍可能收到脱敏文本。

**影响**

- protected 索引可能保存 display 内容，而不是真正的 protected 原文。
- 后续下载、原文查看和 protected 检索证据链不一致。
- 违反 `03 Knowledge 回源 API` 的冻结契约。

**修复建议**

1. 保持 RAG 请求契约不变，继续使用 `content_variant=display|original`。
2. 在 Knowledge Repository 中让 `GetKnowledgeContent` 按 variant 选择内容源：
   - `display` 读取 `messages.normalized_content`。
   - `original` 优先读取 `message_private_content`。
   - 没有 private content 时，明确返回原文不可用，不能静默回退到 display。
3. `purpose=index` 仍只允许 RAG 服务身份读取 protected 原文。
4. 普通用户读取 protected 原文仍必须经过现有审批和授权逻辑。
5. 不重新引入旧 `memory_*` 业务逻辑。

**修复后验收**

1. 构造一条 `normalized_content != message_private_content.content` 的敏感消息。
2. 分别请求 display 和 original：
   - display 必须返回脱敏内容。
   - original 必须返回原始内容。
3. 完成 RAG Parse 后，检查 ES protected 文档正文必须等于原始内容。
4. display 文档正文必须仍为脱敏内容。
5. 普通用户请求 original 时仍返回 403 或待审批状态。

### P0-2 公开接口信任客户端组织 Scope，Core 未验证成员关系

**问题**

RAG 公开 API 允许请求体或 `X-Organization-ID` 决定组织：

```text
services/rag/app/routers/api.py:31
services/rag/app/routers/api.py:42
```

但冻结接口明确要求客户端不能传组织，应由当前用户和唯一组织关系生成：

```text
services/rag/docs/interfaces/05-rag-public-api.md:39
services/rag/docs/interfaces/05-rag-public-api.md:47
```

Core 授权接口又直接将请求中的组织加入授权范围：

```text
services/core/internal/httpapi/authorization_handler.go:122
```

同时 OpenFGA `ListObjects` 忽略 `organizationID`，且 `truncated` 始终返回 `false`：

```text
services/core/internal/infrastructure/openfga/client.go:47
services/core/internal/httpapi/authorization_handler.go:137
```

**影响**

- 只要知道另一个组织的 UUID，就可能让 RAG 按该组织 Scope 发起检索。
- 当前返回的 `authorized_organization_ids` 不能证明用户确实属于该组织。
- 超大数据量下 `truncated=false` 会让调用方误以为授权集合完整。
- 跨租户隔离和 `scope_key` 安全边界不可验收。

**修复建议**

1. RAG 公开搜索接口不接受客户端提供的权威 `organization_id`。
2. 仅允许网关注入可信 `X-User-ID`，组织归属由 Core 根据 subject 推导。
3. Core 增加或收口一个内部“解析当前用户唯一组织”的能力：
   - 校验用户组织成员关系。
   - 返回唯一的 `organization_id`。
   - 多组织、无组织或成员关系不可确认时失败关闭。
4. Core Scope 接口不能再直接回显请求组织，必须使用已校验的成员关系。
5. `ListObjects` 必须按组织上下文约束结果。
   - 如果 OpenFGA API 支持 contextual tuples 或组织上下文，优先使用。
   - 如果不支持，则在 Core 对返回对象进行组织归属过滤或二次批量 `Check`。
6. `truncated` 必须反映真实截断状态。
7. 授权后端不可用、结果截断或组织归属不确定时：
   - RAG 返回 `503 authz_unavailable` 或 `403 forbidden`。
   - 禁止降级为全组织检索。
8. 继续遵守冻结决策，不为 MVP 新增 OpenFGA `knowledge_base` 类型。

**修复后验收**

1. 用户 A 属于组织 A，构造请求并修改 body 或 Header 为组织 B：
   - 必须返回 403 或 422。
   - 不得检索到组织 B 的任何结果。
2. 用户不属于任何组织时，组织搜索必须失败关闭。
3. 用户属于且仅属于一个组织时，服务端可正确推导 Scope。
4. OpenFGA 返回截断时，RAG 必须失败关闭。
5. protected 原文和附件权限检查使用同一个已校验组织上下文。

### P0-3 ES 写入失败后无法正常重试

**问题**

Index Service 每次只读取 `embedding_status=pending` 的 Chunk：

```text
services/rag/app/application/index_service.py:37
```

ES 写入失败时，Chunk 被标记为 `failed` 后抛错：

```text
services/rag/app/application/index_service.py:86
```

下一次 Index 重试仍进入 Index Lane，但 Chunk 不再是 `pending`，因此可能没有
可处理数据，最终被当成 `metadata_only` 成功终态。

**影响**

- 真实 ES 故障会被掩盖为“无需索引”。
- Knowledge 可能收到错误的 `metadata_only` 回调。
- 后续搜索缺少本应存在的文档。
- Phase 7 的失败恢复和重建能力不成立。

**修复建议**

1. 明确 Chunk 索引状态机，例如：

```text
pending -> indexing -> ready
pending -> indexing -> failed -> retryable
```

2. 不要把 Chunk 直接永久标记为 `failed`，除非已经达到终态重试上限。
3. Index Service 的重试查询必须包含允许重试的 `failed` Chunk。
4. `metadata_only` 只能用于 Parse 已明确判定为 metadata-only 的任务。
5. 如果存在 Chunk，但 Embedding 或 ES 写入失败，任务必须保持可重试或最终
   `failed`，不能返回 `metadata_only`。
6. 为 ES 写入增加幂等性：
   - 使用确定性的 `_id`。
   - 重试同一 Chunk 不产生重复文档。
7. 增加失败注入测试。

**修复后验收**

1. 第一次 ES 写入失败，第二次成功：
   - Job 最终 `ready`。
   - Chunk 最终 `ready`。
   - ES 中存在唯一文档。
2. ES 持续失败并超过最大重试次数：
   - Job 最终 `failed`。
   - Knowledge 收到 `failed`，不是 `metadata_only`。
3. 真实 metadata-only 附件仍保持成功终态。
4. 重复执行 Index Lane 不产生重复 ES 文档。

## 5. P1 问题与修复

### P1-1 长任务没有续租

**问题**

Repository 已提供 `heartbeat`，配置也包含 `RAG_TASK_HEARTBEAT_SECONDS`，但运行时没有
调用：

```text
services/rag/app/application/runtime.py:17
services/rag/app/infrastructure/persistence/mvp.py:220
```

MinerU 解析、大附件处理和慢 Embedding 可能超过默认 300 秒租约。

**影响**

- 另一个 Worker 可以在第一个 Worker 仍在运行时重新领取任务。
- Parse 和 Index 可能重复执行。
- 任务状态、Attempt 和回调顺序不可信。

**修复建议**

1. 在 Parse、Index、Memory Lane 执行期间增加后台续租。
2. 续租间隔使用 `RAG_TASK_HEARTBEAT_SECONDS`，必须明显小于
   `RAG_TASK_LEASE_SECONDS`。
3. Lane 完成、失败或抛出异常时，在 `finally` 中停止续租。
4. 租约续租必须校验当前 `lease_owner` 或 fencing token。
   - 旧 Worker 不能续租已经被其他 Worker 抢走的任务。
5. 为长时间操作增加可测试的 fake 延迟，不依赖真实 MinerU。

**修复后验收**

1. 模拟处理时长大于一个完整租约，另一个 Worker 不能同时领取任务。
2. Worker 正常完成后可立即释放或自然过期租约。
3. 原 Worker 失去租约后，续租不能覆盖新 Worker 的租约。
4. 进程崩溃后，任务仍可在租约过期后被恢复。

### P1-2 owner_only 私人任务初始 Scope 错误

**问题**

Dispatcher 使用事件 envelope 的组织或零 UUID 生成 `scope_id`：

```text
services/rag/app/infrastructure/persistence/mvp.py:131
```

但 `knowledge.ready` 当前 payload 没有权威的 `scope_type` 和 `scope_id`：

```text
services/rag/docs/interfaces/01-knowledge-ready-event.md
```

初始任务、`processing` 回调和失败任务可能使用零 UUID。只有 Parse 回源后才会尝试修正。

**影响**

- 私人内容的初始 processing 状态 Scope 错误。
- Parse 前失败时，任务无法可靠归入私人 Scope。
- 回调、审计和恢复任务可能携带错误 Scope。
- 未来按 Scope 调度或清理任务时存在串 Scope 风险。

**修复建议**

1. 在 `knowledge.ready` payload 中增加权威 Scope 字段，二选一：

```text
scope_type=user|organization
scope_id=<owner_user_id 或 organization_id>
```

或：

```text
knowledge_scope
owner_user_id
organization_id
```

2. RAG Dispatcher 必须优先使用事件中的权威 Scope。
3. `owner_only` 缺少 owner user ID 时拒绝创建任务，不能回退到零 UUID。
4. Parse 回源结果仍做版本和 Scope 一致性校验。
5. 修改服务二投递事件时保持原有 `event_id` 幂等规则。

**修复后验收**

1. owner_only 私人消息从 Dispatcher 开始就是 `scope_type=user`、
   `scope_id=owner_user_id`。
2. 组织消息从 Dispatcher 开始就是正确的 organization Scope。
3. 事件缺少 owner user ID 时任务拒绝进入处理链路。
4. Parse 返回不同 Scope 时必须失败，不能静默覆盖。

### P1-3 AI 问答没有真正写入 QA 历史

**问题**

`/ai/documents` 和 `/ai/documents/stream` 直接构造回答，没有调用
`add_qa_message`，也没有返回完整会话消息 ID：

```text
services/rag/app/routers/api.py:126
services/rag/app/routers/api.py:157
```

冻结接口要求返回：

```text
conversation_id
user_message_id
assistant_message_id
```

**影响**

- QA 会话历史为空或不完整。
- 前端无法稳定关联用户问题和回答。
- 流式失败无法更新 assistant 消息状态。
- 冻结的 QA 接口不能验收。

**修复建议**

1. 请求携带 `conversation_id` 时校验会话属于当前用户和当前 Scope。
2. 未携带时创建新会话。
3. 回答前写入 user message。
4. 回答完成后写入 assistant message，并返回消息 ID。
5. 流式回答先创建 assistant pending 消息，`done` 时更新为 completed。
6. 流式异常将 assistant message 更新为 failed，并保存安全错误信息。
7. citations 和检索诊断信息按接口契约保存。

**修复后验收**

1. 普通和流式问答都返回 `conversation_id`、`user_message_id`、
   `assistant_message_id`。
2. 查询会话历史时能看到 user 和 assistant 两条消息。
3. 流式中断后历史中存在 failed assistant 消息。
4. 用户不能读取或继续其他人的会话。

### P1-4 候选审核 merge 没有保存 Alias

**问题**

审核 `merge` 只保存 `resolved_entity_id`，没有调用 `upsert_alias`：

```text
services/rag/app/infrastructure/persistence/mvp.py:971
```

**影响**

- 候选中已经确认的合并名称不会成为正式 Alias。
- 后续确定性 Entity 匹配无法识别该名称。
- “merge 只添加别名并保留正式 Entity ID”验收项不成立。

**修复建议**

1. `merge` 在同一个数据库事务中：
   - 校验目标 Entity 属于当前 Scope。
   - 使用候选名称创建或更新 Alias。
   - 保存 `resolved_entity_id`。
   - 创建 Branch Refresh Job。
2. Alias 使用规范化后的候选名称，避免重复。
3. 审核幂等重放时不能重复创建 Alias。
4. Alias 保存失败时整个审核事务回滚。

**修复后验收**

1. merge 后能查询到指向目标实体的新 Alias。
2. 使用原候选名称重新处理 Chunk 时能匹配到目标 Entity。
3. 重复提交同一审核请求不增加重复 Alias。
4. 目标 Entity 不属于当前组织时返回 404 或 403。

### P1-5 审核幂等标识缺失时自动生成 UUID

**问题**

审核接口在缺少 `review_request_id` 和 `Idempotency-Key` 时自动生成 UUID：

```text
services/rag/app/routers/admin.py:178
```

冻结接口要求 `review_request_id` 必填，同一请求 ID 只能执行一次：

```text
services/rag/docs/interfaces/07-rag-admin-entity-tree-api.md:224
```

**影响**

- 客户端超时重试会生成新的请求 ID。
- 审核动作可能重复执行。
- 并发写入、Alias 和 Branch Refresh 可能重复。

**修复建议**

1. `review_request_id` 和 `Idempotency-Key` 至少提供一个。
2. 两者同时提供但值不同时返回 422。
3. 禁止服务端在缺失时自动生成请求 ID。
4. 继续使用 `candidate_id + review_request_id` 作为数据库幂等键。
5. 相同请求 ID 返回第一次的完整结果。

**修复后验收**

1. 两者都不传时返回 422，不修改任何数据。
2. 两者相同且在唯一约束范围内时，只能执行一次。
3. 相同请求 ID 重放返回相同结果，不重复刷新 Branch。
4. 不同请求 ID 对已终态候选再次审核时按状态冲突处理。

## 6. P2 问题与修复

### P2-1 候选实体 distinct 统计不准确

**问题**

每次 Candidate mention 冲突只递增 `mention_count`：

```text
services/rag/app/infrastructure/persistence/mvp.py:809
```

`distinct_chunk_count`、`distinct_source_count` 和
`distinct_conversation_count` 基本保持为初始化值。

**影响**

- 管理端来源统计不准确。
- 候选评分和人工审核依据失真。
- `07 候选实体接口` 的列表验收数据不可信。

**修复建议**

1. 优先在读取时从 `entity_candidate_mentions` 聚合：
   - `COUNT(DISTINCT chunk_id)`
   - `COUNT(DISTINCT source_resource_id)`
   - `COUNT(DISTINCT source_conversation_id)`
2. 如果 mention 表缺少 source 维度，补充必要字段后重新生成 Mention 记录。
3. 如保留聚合字段，必须通过事务内重算或可靠事件更新，不能只递增 mention count。
4. 列表、详情和评分使用同一统计来源。

**修复后验收**

1. 同一 Chunk 同一候选重复出现不影响 `distinct_chunk_count`。
2. 同一会话多个 Chunk 增加 chunk 数，但不增加 conversation 数。
3. 不同来源和不同会话分别正确增加。
4. 列表和详情返回一致统计。

### P2-2 Knowledge 内存实现仍使用 succeeded

**问题**

内存实现仍根据 `RAGStatus == "succeeded"` 更新 Vector 状态：

```text
services/knowledge/internal/repository/memory.go:2809
services/knowledge/internal/repository/memory.go:2882
```

生产状态已经改为 `processing/ready/metadata_only/failed`。

**影响**

- 内存测试无法准确反映生产行为。
- 后续基于内存 Store 的测试可能产生假阳性或假阴性。

**修复建议**

1. 将成功判断统一为 `ready`。
2. 明确 `processing`、`metadata_only` 和 `failed` 的 Vector 映射。
3. 补充与 PostgreSQL 行为一致的状态测试。
4. 不借此修改旧 `memory_*` RAG 业务代码，只修正 Knowledge 的 Store 测试语义。

**修复后验收**

1. `ready` 且版本匹配时 VectorStatus 为 `ready`。
2. `failed` 时 VectorStatus 为 `failed`。
3. `metadata_only` 不错误覆盖为 ready 或 failed。
4. 内存实现与 PostgreSQL 实现测试结果一致。

## 7. 建议修复顺序

1. 修复 P0-1 protected 原文回源。
2. 修复 P0-2 组织 Scope 和 Core 成员关系校验。
3. 修复 P0-3 ES 失败重试。
4. 修复 P1-1 租约续期。
5. 补充 `knowledge.ready` 权威 Scope 并修复 P1-2。
6. 修复 QA 历史和流式落库。
7. 修复 merge Alias 和审核幂等。
8. 修复候选统计和 Knowledge 内存状态。
9. 启用真实集成测试并执行最终重新验收。

## 8. 重新验收要求

### 8.1 基础测试

```powershell
cd services/rag
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
```

```powershell
cd services/knowledge
go test ./...
```

```powershell
cd services/core
go test ./...
```

### 8.2 真实集成测试

```powershell
cd services/rag
$env:RAG_MVP_INTEGRATION='1'
.\.venv\Scripts\python.exe -B -m pytest tests/test_mvp_integration.py -q -p no:cacheprovider
```

```powershell
cd services/rag
$env:RAG_CROSS_SERVICE_INTEGRATION='1'
.\.venv\Scripts\python.exe -B -m pytest tests/test_cross_service_integration.py -q -p no:cacheprovider
```

真实集成测试必须实际使用：

```text
PostgreSQL rag_mvp
Elasticsearch rag_chunks_display_read/write
Elasticsearch rag_chunks_protected_read/write
Redis Streams
Knowledge 服务
Core 授权接口
```

### 8.3 P0 专项验收

1. protected 原文与 display 内容不同时，ES protected 文档必须是原文。
2. 用户修改 body 或 Header 指向其他组织时，检索必须失败关闭。
3. ES 第一次失败、第二次成功时，任务最终 `ready`。
4. ES 持续失败时，任务最终 `failed`，不能是 `metadata_only`。
5. 长任务超过一个租约周期时，不能被其他 Worker 重复领取。

### 8.4 P1 专项验收

1. 私人任务从 Dispatcher 开始使用正确 owner Scope。
2. QA 普通和流式接口都写入历史并返回消息 ID。
3. merge 后 Alias 可用于正式 Entity 匹配。
4. 审核缺少幂等 ID 时拒绝执行。

### 8.5 验收通过条件

重新验收通过必须同时满足：

1. P0 问题全部关闭。
2. P1 问题全部关闭。
3. P2 问题已修复，或形成明确的后置项并获得确认。
4. RAG 单测全部通过。
5. Knowledge 和 Core Go 测试全部通过。
6. 两个真实集成测试均已启用并通过。
7. 没有使用旧 `memory_*` RAG 业务链路。
8. 没有旧表、旧索引双读或双写。

## 9. 禁止验收通过的条件

出现以下任一情况时，不得宣布 Phase 7 验收通过：

1. protected 原文与 display 文本完全相同，且没有证明源数据本身相同。
2. 用户可以通过 body 或 Header 访问不属于自己的组织 Scope。
3. ES 写入失败后任务被标记为 `metadata_only`。
4. 长任务租约过期后仍会被其他 Worker 重复领取。
5. QA 接口不落历史或缺少消息 ID。
6. merge 审核不创建 Alias。
7. 审核请求缺少幂等 ID 仍会被执行。
8. 真实跨服务集成测试未运行。

