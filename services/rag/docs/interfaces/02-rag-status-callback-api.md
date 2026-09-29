# 02 RAG 状态回调 API

> 状态：已冻结

## 1. 方向

```text
RAG Callback Lane -> Service2/Knowledge
```

## 2. 接口

```text
POST /internal/knowledge/{knowledge_item_id}/rag-result
```

## 3. Request

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `knowledge_item_id` | `uuid` | 是 | KnowledgeItem ID |
| `source_event_id` | `uuid` | 是 | 原事件 ID |
| `rag_job_id` | `uuid` | 是 | RAG 任务 ID |
| `content_version` | `integer` | 是 | 内容版本 |
| `acl_version` | `integer` | 是 | ACL 版本 |
| `status` | `string` | 是 | `processing/ready/metadata_only/failed` |
| `error_code` | `string/null` | 否 | 稳定错误码 |
| `retryable` | `boolean` | 是 | 是否可重试 |
| `result` | `object` | 否 | Chunk、Branch、诊断等摘要 |
| `occurred_at` | `datetime` | 是 | 状态发生时间 |

## 4. Response

```json
{
  "applied": true,
  "status": "ready",
  "reason": null
}
```

可能 `reason`：

```text
duplicate
stale_version
terminal_state
```

## 5. 规则

- Knowledge 只写 `knowledge_items.rag_status` 及相关元数据。
- Knowledge 从 `knowledge_item_id` 自行派生 `resource_type/resource_id`，不信任 RAG 重复提交的资源身份。
- 终态 `ready/metadata_only/failed` 不能被旧任务覆盖。
- `metadata_only` 是成功终态。
- 回调必须由 RAG Outbox 触发并支持重试。
