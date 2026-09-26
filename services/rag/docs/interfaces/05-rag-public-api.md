# 05 RAG 对外搜索与问答 API

> 状态：已冻结

## 1. 方向

```text
Web/Agent -> RAG
```

服务内路径：

```text
/api/v1/...
```

通过 Nginx 的公开路径：

```text
/api/rag/api/v1/...
```

管理和前端调用必须使用网关公开路径。

## 2. Search Request

```json
{
  "query": "青云项目现在进展",
  "scope_type": "organization",
  "knowledge_base_ids": ["kb-uuid"],
  "top_k": 8,
  "include_protected": true,
  "occurred_after": null,
  "occurred_before": null
}
```

客户端不能传：

```text
organization_id
owner_user_id
scope_key
```

服务端根据当前用户和唯一组织关系生成 Scope。

## 3. 搜索接口

```text
POST /api/v1/search/global
POST /api/v1/search/knowledge
POST /api/v1/search/tree
```

### 差异

| 接口 | 策略 |
|---|---|
| `global` | BM25 + kNN + RRF |
| `knowledge` | BM25 |
| `tree` | Tree Mode + Chunk 检索，当前默认 shadow |

### Search Response

```json
{
  "request_id": "uuid",
  "query": "青云项目现在进展",
  "items": [
    {
      "chunk_id": "sha256",
      "content": "授权后的内容",
      "score": 0.91,
      "source": {
        "knowledge_item_id": "uuid",
        "resource_type": "message",
        "content_variant": "protected",
        "source_conversation_id": "uuid",
        "sent_at": "2026-09-27T10:00:00Z",
        "branch_keys": []
      }
    }
  ],
  "citations": [],
  "diagnostics": {
    "tree_mode": "shadow",
    "execution_path": "chunk_fusion",
    "fallback_reason": null
  }
}
```

不返回：

```text
embedding
auth_object_key
内部 token
下载 URL
```

## 4. AI 问答接口

```text
POST /api/v1/ai/documents
POST /api/v1/ai/documents/stream
```

### JSON Response

```text
request_id
conversation_id
user_message_id
assistant_message_id
answer
citations
items
diagnostics
retrieval_mode
execution_path
```

### SSE Events

```text
event: meta
event: citation
event: token
event: error
event: done
```

## 5. QA 历史接口

```text
GET    /api/v1/qa/conversations
POST   /api/v1/qa/conversations
GET    /api/v1/qa/conversations/{id}
PATCH  /api/v1/qa/conversations/{id}
DELETE /api/v1/qa/conversations/{id}
```

会话必须绑定当前用户和当前 Scope。

## 6. 错误

```text
400 invalid_request
401 unauthorized
403 forbidden
404 conversation_not_found
422 invalid_scope
503 search_unavailable
503 authz_unavailable
503 qa_unavailable
```
