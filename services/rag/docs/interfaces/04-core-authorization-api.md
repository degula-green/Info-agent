# 04 Core 授权 API

> 状态：已冻结

## 1. 方向

```text
RAG -> Core/OpenFGA Adapter
```

RAG 不直接连接 OpenFGA。

## 2. 搜索范围接口

```text
POST /internal/v1/authorization/search-scope
```

### Request

```json
{
  "subject_type": "user",
  "subject_id": "user-uuid",
  "scope_type": "organization",
  "scope_id": "org-uuid",
  "resource_parts": ["original", "content"]
}
```

### Response

```json
{
  "available": true,
  "snapshot_id": "uuid",
  "expires_at": "2026-09-27T10:00:05Z",
  "authorized_organization_ids": ["org-uuid"],
  "authorized_conversation_group_ids": ["conversation-uuid"],
  "authorized_protected_object_keys": [
    "knowledge_original:item-uuid",
    "attachment_content:attachment-uuid"
  ],
  "truncated": false
}
```

规则：

- 普通组织内容使用组织和会话权限分区，不返回全部 KnowledgeItem ID。
- MVP 不新增 OpenFGA KnowledgeBase 类型。
- Core 只负责组织、会话、私人和精确 protected 授权。
- RAG 将客户端请求的 `knowledge_base_ids` 与 Core 授权 Scope 和 ES KB 字段取交集。
- KnowledgeBase 本身在 MVP 中不拥有独立 ACL。
- protected 使用精确对象授权。
- `truncated=true` 时 protected 分支失败关闭。
- 缓存不能超过 `expires_at`，本地建议最长 5 秒。

## 3. 批量权限检查接口

```text
POST /internal/v1/authorization/check-batch
```

### Request

```json
{
  "subject_type": "user",
  "subject_id": "user-uuid",
  "scope_type": "organization",
  "scope_id": "org-uuid",
  "snapshot_id": "uuid",
  "checks": [
    {
      "check_id": "c1",
      "resource_type": "knowledge_item",
      "resource_part": "display",
      "resource_id": "item-uuid",
      "action": "view"
    }
  ]
}
```

### Response

```json
{
  "snapshot_id": "uuid",
  "decisions": [
    {
      "check_id": "c1",
      "allowed": true
    }
  ]
}
```

规则：

- 单次最多 100 个 check。
- 缺项、数量不一致或 Core 异常时全部按拒绝处理。
- 候选进入 RRF/Rerank 前必须检查。
- Prompt 组装前必须再次检查。

## 4. 资源和动作映射

| 内容 | resource_type | resource_part | 动作 |
|---|---|---|---|
| 消息展示正文 | `knowledge_item` | `display` | `view` |
| Knowledge 原文 | `knowledge_item` | `original` | `view` |
| 附件元数据 | `attachment` | `metadata` | `view` |
| 附件正文 | `attachment` | `content` | `view` |
| 原文件下载 | `attachment` | `content` | `download` |
