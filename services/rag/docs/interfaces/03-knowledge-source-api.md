# 03 Knowledge 回源 API

> 状态：已冻结

## 1. 方向

```text
RAG -> Service2/Knowledge
```

## 2. 接口列表

```text
GET /internal/knowledge/{knowledge_item_id}
GET /internal/knowledge/{knowledge_item_id}/content
GET /internal/attachments/{attachment_id}
```

## 3. Query

```text
content_version
acl_version
content_variant=display|original
purpose=index
```

`purpose=index` 只允许 RAG 服务身份使用。

服务端必须：

- 校验服务 Token 和 `X-Caller-Service=rag`。
- 记录 `purpose/resource_id/content_version/job_id` 审计。
- 普通用户接口继续拒绝对 protected 原文的访问。
- `purpose=index` 可以读取受保护变体和内部 object_ref。

## 4. Metadata Response

至少返回：

```text
knowledge_item_id
knowledge_base_id
knowledge_scope
access_scope
owner_user_id
organization_id
source_type
source_conversation_id
source_conversation_type
source_audience_policy
external_conversation_id
source_message_id
source_attachment_id
sender_identity_id
sender_platform
sender_workspace_key
sender_external_user_id
sender_mapped_user_id
sender_display_name
content_type
content_hash
content_version
acl_version
content_access_required
lifecycle_status
```

附件额外返回：

```text
attachment_id
file_name
mime_type
size_bytes
object_ref
content_status
```

## 5. Content Response

```text
knowledge_item_id
content_version
content_variant
content_hash
text
```

## 6. 错误

| HTTP | code | 含义 |
|---:|---|---|
| 401 | `unauthorized` | 服务认证失败 |
| 403 | `forbidden` | 无权读取指定变体 |
| 404 | `knowledge_not_found` | 资源不存在 |
| 409 | `knowledge_version_mismatch` | 版本不匹配 |
| 409 | `knowledge_acl_version_mismatch` | ACL 版本不匹配 |
| 503 | `knowledge_unavailable` | 回源暂时不可用 |
