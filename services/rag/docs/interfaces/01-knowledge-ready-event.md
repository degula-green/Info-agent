# 01 `knowledge.ready` 事件

> 状态：已冻结

## 1. 方向

```text
Service2/Knowledge -> Redis Stream -> RAG Dispatcher
```

## 2. Event Envelope

```json
{
  "event_id": "uuid",
  "event_type": "knowledge.ready",
  "event_version": 1,
  "schema_version": 1,
  "occurred_at": "2026-09-27T10:00:00Z",
  "trace_id": "trace-id",
  "organization_id": "uuid-or-empty",
  "producer": "module-2",
  "payload": {}
}
```

## 3. Payload

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `resource_type` | `string` | 是 | `message` 或 `attachment` |
| `resource_id` | `uuid` | 是 | 消息 ID 或附件 ID |
| `knowledge_item_id` | `uuid` | 是 | KnowledgeItem ID |
| `source_conversation_id` | `uuid/null` | 平台会话必填 | Knowledge 内部会话 UUID |
| `source_conversation_type` | `string/null` | 否 | `group` 或 `private` |
| `source_audience_policy` | `string` | 是 | 来源受众策略 |
| `content_version` | `integer` | 是 | 内容版本 |
| `acl_version` | `integer` | 是 | ACL 版本 |
| `content_hash` | `string` | 是 | 权威内容 SHA-256 |
| `content_access_required` | `boolean` | 是 | 是否 protected |

`source_audience_policy`：

```text
source_conversation_members
organization_members
owner_only
```

## 4. 幂等

- `event_id` 全局唯一。
- 同一 `event_id` payload 改变时拒绝。
- 普通任务由 `knowledge_item_id + resource_type + resource_id + content_version` 去重。
- 一个事件对应一个资源和一个任务，RAG 内部负责处理 display/protected 变体。
- 事件不携带 `content_variant`，避免把一个资源错误拆成多个任务。

## 5. 响应

Redis Stream 事件没有同步响应。

可能结果：

```text
ACK 并创建任务
ACK 并忽略重复事件
拒绝 payload 并 ACK
数据库暂时失败，不 ACK
```
