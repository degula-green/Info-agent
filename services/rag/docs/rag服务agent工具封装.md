# RAG 服务 Agent 工具封装方案

## 文档状态

- 版本：v1.1
- 状态：已实施，默认关闭
- 目标：为 Agent 提供可组合的来源定位和内容检索工具
- 范围：RAG 新接口、Agent 执行上下文、两个只读 Capability
- 非目标：历史数据回填、元数据自动刷新、资源审批写入
- 最后更新：2026-10-01

## 1. 结论

工具拆分方向保持不变：

```text
knowledge.search_sources
-> 根据发送人、群聊、时间、类型定位资源

knowledge.search_content
-> 在指定资源或知识库内检索内容

组合调用
-> search_sources
-> resource_ids
-> search_content
```

但 v1.0 不能直接实施，必须先解决：

1. Agent Capability 当前没有可信 `ExecutionContext`。
2. `search_sources` 当前没有为跨步骤绑定提供 `resource_ids`。
3. 空 query 的 metadata-only 查询还不存在。
4. `attachment_types`、`message_types` 当前没有完整索引字段。
5. 当前无法提供文档中的精确 total、matched_fields 和 Top 3 聚合。
6. 字段补齐只覆盖新数据，不能把旧数据当作已覆盖。

## 2. Agent 执行上下文

### 2.1 当前问题

当前 Capability 协议是：

```python
def validate(self, arguments: dict[str, Any]) -> BaseModel:
    ...

def execute(self, arguments: BaseModel) -> dict[str, Any]:
    ...
```

`execute()` 没有 Task、用户或组织上下文。

`TaskRecord.owner_user_id` 只存在于 Kernel，不会自动传给 Capability。

### 2.2 禁止项

以下字段不能出现在 Planner 可见的 Tool Schema 中：

```text
user_id
scope_id
organization_id
Authorization
X-User-ID
X-Organization-ID
```

模型不能决定以哪个用户或组织身份检索。

### 2.3 推荐实现

在 Agent Kernel 增加受信任的执行上下文：

```python
@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    plan_id: str
    step_id: str
    owner_user_id: str
    organization_id: str | None
    request_id: str
    trace_id: str
```

推荐通过 `contextvars.ContextVar` 注入：

```python
_execution_context: ContextVar[ExecutionContext | None]

def current_execution_context() -> ExecutionContext:
    ...
```

`CapabilityExecutor.execute()` 在调用能力前设置上下文：

```python
context = ExecutionContext(
    task_id=task.task_id,
    plan_id=plan.plan_id,
    step_id=step.step_id,
    owner_user_id=task.owner_user_id,
    organization_id=resolve_organization(task),
    request_id=call.request_id,
    trace_id=trace_id,
)

with execution_context(context):
    output = capability.execute(arguments)
```

这样不需要修改所有现有 Capability 的方法签名。

### 2.4 组织 ID 来源

组织 ID 只能来自可信 Task 上下文或 Core 查询，不能由 Planner 填写。

允许来源：

- Task `source_ref`
- Task `input` 中由入口服务写入的受信任上下文字段
- Core 当前用户组织查询

进入 RAG 时由 Agent Capability 转成：

```text
X-User-ID
X-Organization-ID
```

## 3. 字段和索引前置条件

`sender`、`conversation_name`、`source_platform` 已在字段补齐改造中加入新数据链路。

仍需要补充：

| 字段 | 用途 | 来源 |
|---|---|---|
| `message_type` | 区分 text/file/image/video | `knowledge.messages.message_type` |
| `file_extension` | 按 pdf/excel/word/ppt/image 过滤 | `file_name` 规范化为小写扩展名 |
| `mime_type` | 文件类型兜底过滤 | `knowledge.attachments.mime_type` |

建议索引：

```json
{
  "message_type": {
    "type": "keyword"
  },
  "file_extension": {
    "type": "keyword"
  },
  "mime_type": {
    "type": "keyword"
  }
}
```

`attachment_types` 在 Agent 层转换为扩展名集合：

```text
pdf   -> pdf
excel -> xlsx, xls, csv
word  -> docx, doc
ppt   -> pptx, ppt
image -> png, jpg, jpeg, gif, webp, bmp
other -> 不属于以上集合
```

### 3.1 数据覆盖门槛

新工具只能处理已补齐元数据的数据。

建议增加配置：

```text
RAG_AGENT_METADATA_TOOLS_ENABLED=false
```

开放条件：

- 新消息的 sender 字段覆盖率达到目标。
- 新聊天附件的 sender 和 conversation 字段覆盖率达到目标。
- 当前环境明确知道历史数据未回填。

在历史数据未回填前，工具结果必须声明：

```text
metadata_coverage=partial
```

不能把旧数据缺失解释成“没有匹配结果”。

## 4. 新增 RAG 接口

### 4.1 来源定位

```http
POST /api/v1/search/sources
```

用途：

- 谁发过什么
- 某群有哪些资源
- 某时间范围内有哪些消息或文件
- 先定位资源，再查找内容

#### 请求体

```json
{
  "query": "预算",
  "sender_ids": ["user-123"],
  "sender_names": ["张三"],
  "conversation_ids": ["conv-456"],
  "conversation_names": ["财务群"],
  "resource_types": ["message", "attachment"],
  "file_extensions": ["xlsx"],
  "message_types": ["text", "file"],
  "occurred_after": "2026-08-01T00:00:00+08:00",
  "occurred_before": "2026-08-31T23:59:59+08:00",
  "knowledge_base_ids": ["kb-001"],
  "top_k": 20,
  "include_protected": false
}
```

请求头由 Agent Capability 注入：

```text
X-User-ID
X-Organization-ID
X-Request-ID
X-Trace-ID
```

#### 过滤语义

- `sender_ids` 和 `sender_names` 组成一个 OR 组。
- `conversation_ids` 和 `conversation_names` 组成一个 OR 组。
- 不同 OR 组之间为 AND。
- 时间范围与其他过滤条件为 AND。
- 数组内部是 OR。

例如：

```text
(sender_id IN ... OR sender_name MATCH ...)
AND
(conversation_id IN ... OR conversation_name MATCH ...)
AND
sent_at BETWEEN ...
```

#### 空 query 行为

`query` 可为空。

- query 非空：BM25 + 元数据过滤
- query 为空：metadata-only 查询

metadata-only 查询使用过滤条件筛选并按 `sent_at DESC` 排序。

不能继续复用当前强制 `multi_match` 的 BM25 查询。

#### 响应体

```json
{
  "request_id": "req-abc123",
  "items": [
    {
      "resource_id": "res-123",
      "resource_type": "attachment",
      "knowledge_item_id": "item-456",
      "title": "2026年预算表.xlsx",
      "file_name": "预算表.xlsx",
      "message_type": "file",
      "sender": {
        "id": "user-123",
        "name": "张三",
        "platform": "feishu"
      },
      "conversation": {
        "id": "conv-456",
        "name": "财务项目群",
        "type": "group",
        "platform": "feishu"
      },
      "knowledge_base": {
        "id": "kb-001"
      },
      "sent_at": "2026-08-20T10:20:30+08:00",
      "score": 0.0162,
      "score_type": "rrf",
      "preview": "总预算500万元，其中Q1预算200万..."
    }
  ],
  "resource_ids": ["res-123"],
  "returned_count": 1,
  "has_more": false,
  "diagnostics": {
    "candidate_count": 20,
    "authorized_count": 1,
    "metadata_coverage": "partial",
    "execution_path": "metadata_filter"
  }
}
```

明确限制：

- 不返回 `matched_fields`，v1 没有可靠的字段命中追踪。
- 不把 RRF 分值伪装成 0-1 相似度。
- `knowledge_base.name` 需要 Knowledge 解析，v1 只返回 ID。
- `returned_count` 是授权后返回数量，不等同于全局精确总数。
- `has_more` 基于候选窗口和 top_k 判断。

### 4.2 内容检索

```http
POST /api/v1/search/content
```

用途：

- 文档里写了什么
- 在 search_sources 定位的资源内搜索内容

#### 请求体

```json
{
  "query": "违约条款 赔偿责任",
  "resource_ids": ["res-123"],
  "knowledge_base_ids": ["kb-001"],
  "top_k": 10,
  "include_protected": false,
  "group_by_source": true
}
```

`resource_ids` 过滤必须进入 ES `filter`：

```json
{
  "terms": {
    "resource_id": ["res-123", "res-456"]
  }
}
```

如果同时需要区分 message 和 attachment，增加 `resource_types` 过滤。

#### 聚合模式

`group_by_source=true`：

```json
{
  "request_id": "req-xyz789",
  "items": [
    {
      "resource_id": "res-123",
      "resource_type": "attachment",
      "title": "采购合同V2.pdf",
      "sender": {
        "id": "user-123",
        "name": "张三",
        "platform": "feishu"
      },
      "conversation": {
        "id": "conv-456",
        "name": "法务群",
        "type": "group",
        "platform": "feishu"
      },
      "sent_at": "2026-07-15T14:30:00+08:00",
      "best_score": 0.0161,
      "score_type": "rrf",
      "matched_chunk_count": 5,
      "chunks": [
        {
          "chunk_id": "chunk-001",
          "text": "第八条 违约责任...",
          "score": 0.0161,
          "position": {
            "page_number": 8,
            "paragraph_index": 12
          }
        }
      ]
    }
  ],
  "returned_source_count": 1,
  "returned_chunk_count": 1,
  "has_more": false,
  "diagnostics": {
    "bm25_count": 8,
    "knn_count": 10,
    "rrf_merged": 12,
    "metadata_coverage": "partial"
  }
}
```

聚合规则：

- 按 `resource_type + resource_id + content_version` 聚合。
- 每个资源最多保留 3 个 chunk。
- `matched_chunk_count` 是当前候选窗口内匹配数，不是全库精确总数。
- 聚合必须在最终 top_k 截断前执行。
- 先处理 display/protected 去重，再聚合。

### 4.3 不新增资源元数据接口

`GET /api/v1/resources/{resource_id}/metadata` 本期不做。

原因：

- RAG 当前没有按任意 resource ID 查询完整 PostgreSQL 资源的稳定端口。
- ES 只能返回已索引 Chunk 元数据，不能保证文件大小、MIME、状态等信息完整。
- 该能力后续应通过 Knowledge 资源查询接口实现。

## 5. Agent Tool 契约

### 5.1 knowledge.search_sources

#### Planner 输入

```python
class SearchSourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, max_length=200)
    sender_names: list[str] = Field(default_factory=list, max_length=10)
    conversation_names: list[str] = Field(default_factory=list, max_length=10)
    occurred_after: str | None = None
    occurred_before: str | None = None
    attachment_types: list[str] = Field(default_factory=list, max_length=5)
    message_types: list[str] = Field(default_factory=list, max_length=5)
    top_k: int = Field(default=20, ge=1, le=50)
```

不允许出现 user、organization、scope 字段。

#### 输出

```python
class SourceInfo(BaseModel):
    resource_id: str
    resource_type: str
    title: str
    sender_id: str | None
    sender_name: str | None
    sender_platform: str | None
    conversation_id: str | None
    conversation_name: str | None
    conversation_type: str | None
    conversation_platform: str | None
    sent_at: str | None
    preview: str | None
    score: float
    score_type: str


class SearchSourcesOutput(BaseModel):
    sources: list[SourceInfo]
    resource_ids: list[str]
    returned_count: int
    has_more: bool
    summary: str
    metadata_coverage: str
```

`resource_ids` 是跨步骤绑定的稳定字段，不能只放在 `sources[]` 内。

#### Descriptor

```python
CapabilityDescriptor(
    name="knowledge.search_sources",
    description=(
        "根据发送人、群聊、时间和资源类型定位知识来源（只读）。"
        "适合回答谁发过什么、在哪个群、什么时间发送。"
    ),
    input_schema=SearchSourcesInput.model_json_schema(),
    output_schema=SearchSourcesOutput.model_json_schema(),
    risk_level="read_only",
    side_effect=False,
    requires_approval=False,
    idempotent=True,
    timeout_seconds=30,
)
```

#### 执行逻辑

Capability 通过 `current_execution_context()` 获取用户和组织：

```python
context = current_execution_context()

response = self.rag_client.post(
    "/api/v1/search/sources",
    json=request_body,
    headers={
        "X-User-ID": context.owner_user_id,
        "X-Organization-ID": context.organization_id,
        "X-Request-ID": context.request_id,
        "X-Trace-ID": context.trace_id,
    },
)
```

### 5.2 knowledge.search_content

#### Runtime 输入

```python
class SearchContentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    resource_ids: list[str] = Field(default_factory=list, max_length=20)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=5)
    top_k: int = Field(default=10, ge=1, le=50)
```

#### Planner 输入

```python
class SearchContentPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    resource_ids_ref: StepOutputRef | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list)
    top_k: int = 10
```

#### 跨步骤绑定

```python
input_bindings=[
    CapabilityInputBinding(
        planner_argument="resource_ids_ref",
        runtime_argument="resource_ids",
        source_capability="knowledge.search_sources",
        source_output="resource_ids",
    )
]
```

没有这个绑定，Planner 无法可靠执行：

```text
search_sources -> search_content
```

#### 输出

```python
class ContentChunk(BaseModel):
    chunk_id: str
    text: str
    score: float
    score_type: str
    position: dict[str, Any] | None


class ContentResult(BaseModel):
    resource_id: str
    resource_type: str
    title: str
    sender_name: str | None
    conversation_name: str | None
    sent_at: str | None
    best_score: float
    score_type: str
    matched_chunk_count: int
    chunks: list[ContentChunk]


class SearchContentOutput(BaseModel):
    results: list[ContentResult]
    returned_source_count: int
    returned_chunk_count: int
    has_more: bool
    summary: str
    metadata_coverage: str
```

## 6. RAG 查询实现

### 6.1 SearchRequest 扩展

增加：

```python
sender_ids: tuple[str, ...] = ()
sender_names: tuple[str, ...] = ()
conversation_ids: tuple[str, ...] = ()
conversation_names: tuple[str, ...] = ()
resource_ids: tuple[str, ...] = ()
resource_types: tuple[str, ...] = ()
file_extensions: tuple[str, ...] = ()
message_types: tuple[str, ...] = ()
group_by_source: bool = False
```

保留现有时间字段和兼容字段。

### 6.2 ES Filter

精确过滤：

```json
{
  "terms": {
    "sender_identity_id": ["..."]
  }
}
```

```json
{
  "terms": {
    "source_conversation_id": ["..."]
  }
}
```

```json
{
  "terms": {
    "resource_id": ["..."]
  }
}
```

```json
{
  "terms": {
    "file_extension": ["xlsx", "xls"]
  }
}
```

姓名和会话名称：

- 优先使用联系人/Knowledge 解析为 ID。
- 名称匹配只作为 OR 组内的候选召回。
- 不把 `fuzziness=AUTO` 作为准确率保证。

### 6.3 metadata-only Query

当 query 为空时：

```json
{
  "bool": {
    "filter": [
      "..."
    ]
  }
}
```

排序：

```json
[
  {
    "sent_at": {
      "order": "desc",
      "missing": "_last"
    }
  }
]
```

有 query 时才执行 BM25/KNN/RRF。

### 6.4 SourceHit 聚合

```python
def group_by_source(
    results: list[SearchResult],
    *,
    max_chunks_per_source: int = 3,
) -> list[SourceHit]:
    ...
```

顺序：

```text
候选检索
-> display/protected 去重
-> 授权
-> 按资源聚合
-> 每资源保留 Top 3
-> 按 best_score 排序
-> 最终 top_k 截断
```

## 7. 实施阶段

### Phase 1：Agent 可信上下文和绑定

- 增加 `ExecutionContext` 和 `ContextVar`。
- `CapabilityExecutor` 调用能力前注入上下文。
- `SearchSourcesOutput` 增加 `resource_ids`。
- 定义 `knowledge.search_content` 的 Planner 输入和 binding。
- 增加上下文泄漏和跨用户调用测试。

### Phase 2：RAG 元数据过滤

- 增加 `SearchRequest` 过滤字段。
- 增加 sender、conversation、resource、message type、file extension 过滤。
- 增加空 query 的 metadata-only 查询。
- 保留现有 branch 过滤逻辑。
- 增加权限失败、空结果、时间范围测试。

### Phase 3：SourceHit 聚合

- 实现按资源聚合。
- 每资源保留 Top 3 chunks。
- 增加 `matched_chunk_count`，定义为候选窗口内计数。
- 实现 `/search/content`。
- 不修改现有 `/search/global` 的默认行为。

### Phase 4：工具注册和灰度

- 注册两个 Capability。
- 添加 `RAG_AGENT_METADATA_TOOLS_ENABLED` 开关。
- 先对新补字段数据开放。
- 监控无结果率、权限拒绝率和 metadata 缺失率。

## 8. 暂不实施

1. 历史数据回填。
2. 元数据自动刷新事件。
3. metadata-only 更新 ES 而不重新索引。
4. `GET /resources/{resource_id}/metadata`。
5. 精确全局 total。
6. `matched_fields`。
7. 姓名拼音和独立联系人别名表。
8. 修改现有 `/search/global` 的默认响应。

元数据自动刷新需要独立方案解决：

- Ready Item 幂等；
- Outbox 唯一约束；
- 只更新 ES 元数据而不重新 chunk/embedding。

## 9. 验收标准

- Planner 输入中不存在 user、scope、organization 字段。
- Capability 从可信上下文获得 owner 和 organization。
- "张三上个月发过哪些文件" 可单步调用 `search_sources`。
- "张三发的预算文件里写了什么" 可按 binding 自动执行两步。
- 空 query 的纯元数据查询可返回结果。
- 资源 ID 过滤正确。
- 同一资源最多返回 3 个 chunks。
- 旧数据缺失时返回 `metadata_coverage=partial`。
- 权限校验失败时不返回任何未授权内容。
- 现有 `/search/global`、`/search/knowledge`、`/ai/documents` 行为不变。

## 10. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| Agent 上下文注入错误 | 查错用户数据 | 只能由 Task Kernel 注入，禁止 Planner 填写 |
| 历史数据无 sender | 工具漏召回 | feature flag、partial coverage、后续回填 |
| 空 query 处理错误 | “谁发过什么”无结果 | 独立 metadata-only query |
| 聚合过早截断 | 丢掉同资源高价值 chunk | 先聚合去重，再最终 top_k |
| RRF 分值误解 | Agent 误判相似度 | 返回 `score_type=rrf` |
| 姓名模糊匹配误召回 | 返回同名人的结果 | 优先解析 ID，姓名仅召回 |
