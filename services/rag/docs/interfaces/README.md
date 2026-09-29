# RAG 服务 MVP 接口契约

> 状态：已冻结
>
> 范围：`services/rag` 对外 API、跨服务接口和内部端口

## 1. 接口分组

```text
01 knowledge.ready 事件
02 RAG 状态回调 API
03 Knowledge 回源 API
04 Core 授权 API
05 RAG 对外搜索与问答 API
06 RAG 内部端口
07 候选实体审核与树结构管理 API
```

## 2. 统一约定

### 2.0 跨服务最小改动

允许并只允许以下最小跨服务修改：

- Knowledge：修改 `knowledge.ready` 投递字段。
- Knowledge：支持 `purpose=index` 的 RAG 内部回源。
- Knowledge：回调状态改为 `processing/ready/metadata_only/failed`。
- Core：返回组织、会话和 protected 授权范围。
- Core：复用现有组织管理员/信息管理员完成 Entity 审核能力判断。

MVP 不新增 OpenFGA `knowledge_base` 类型。KnowledgeBase 由 RAG 与授权 Scope 取交集过滤。

### 2.1 ID 和时间

- 所有 UUID 使用字符串传输。
- 所有时间使用 ISO-8601。
- 客户端不能提交权威 `scope_key`。
- 单用户单组织模型下，服务端根据当前用户生成 Scope。

### 2.2 服务间认证

内部接口统一携带：

```text
Authorization: Bearer <service-token>
X-Caller-Service: rag | knowledge
X-Request-ID: <uuid>
X-Trace-ID: <uuid>
```

### 2.3 错误格式

```json
{
  "code": "stable_error_code",
  "message": "safe error message",
  "retryable": false,
  "request_id": "uuid"
}
```

### 2.4 权限原则

- 权限失败关闭。
- 无权限内容不能进入候选、RRF、Rerank 或 Prompt。
- 候选 Entity 不进入正式树。
- 管理接口需要能力权限，而不是只判断角色名称。
