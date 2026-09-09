# 服务一与服务三 RAG 授权接口约定

## 1. 目标与边界

服务一是 OpenFGA 的唯一持有者和执行者；服务三只通过内部 HTTP 接口获取授权结果，不安装 OpenFGA SDK、不读取 OpenFGA、不写入权限关系。

服务三调用服务一的目的：

1. 在查询受保护 ES 索引前取得用户可检索的敏感对象范围；
2. 对 ES 候选结果批量检查 `view`；
3. 在 AI 组装提示词前再次检查最终结果。

公开/脱敏内容不进入 `search-scope`，直接查询展示索引；附件元数据与附件正文是两套独立权限。

## 2. 资源映射（不修改当前 model.fga）

| 服务三字段 | OpenFGA 对象/关系 | ES 索引 |
|---|---|---|
| `knowledge_item` + `display` | `knowledge_item:{id}#view` | display |
| `knowledge_item` + `original` | `knowledge_original:{id}#view` | protected |
| `attachment` + `metadata` | `attachment_meta:{id}#view` | display |
| `attachment` + `content` | `attachment_content:{id}#view` | display 或 protected |

`download` 只用于文件下载，不用于 RAG 检索；RAG 检索和 AI 上下文统一检查 `view`。

当前模型中 `knowledge_original` 和受保护 `attachment_content` 使用用户级 `viewer/downloader` 关系。因此，服务二提供的群聊成员和安全管理员用户 ID 由服务一转换为用户级关系；“权限组”是服务一内部同步抽象，不是新增的 OpenFGA 类型。

## 3. 服务间认证与通用字段

所有接口使用：

```http
Authorization: Bearer <RAG_AUTHZ_API_TOKEN>
X-Caller-Service: rag
X-Request-ID: <request-id>
X-Trace-ID: <trace-id>
Content-Type: application/json
```

服务一必须校验服务 Token、`X-Caller-Service=rag` 和请求格式。`subject_id` 必须是已认证的内部用户 ID，不能信任前端自行指定的用户身份。`organization_id` 由调用上下文传入；私人资源请求可为 `null`。

## 4. 获取受保护检索范围

### 请求

```http
POST /internal/v1/authorization/search-scope
```

```json
{
  "subject_type": "user",
  "subject_id": "user_001",
  "organization_id": "org_001",
  "resource_parts": ["original", "content"],
  "knowledge_base_id": null
}
```

`resource_parts` 只允许：`original`、`content`。服务一内部将其映射为：

```text
original → knowledge_original#view
content  → attachment_content#view
```

`knowledge_base_id` 是可选筛选条件。它不是当前 OpenFGA 模型中的关系；服务一能按知识库筛选时可以使用，不能使用时由服务三继续用 ES 的 `knowledge_base_id` 过滤。

### 成功响应

```json
{
  "available": true,
  "snapshot_id": "snap_001",
  "expires_at": "2026-09-05T12:00:30Z",
  "objects": {
    "knowledge_original": ["knowledge_original:ki_001"],
    "attachment_content": ["attachment_content:att_001"]
  }
}
```

`objects` 只返回受保护对象，不返回公开 `knowledge_item` 或 `attachment_meta` 对象。服务一内部可使用 OpenFGA `ListObjects(view)` 生成集合，返回的对象键必须与 ES 的 `auth_object_key` 完全一致。

服务三将对象集合转换为 protected ES 的过滤条件：

```json
{
  "terms": {
    "auth_object_key": [
      "knowledge_original:ki_001",
      "attachment_content:att_001"
    ]
  }
}
```

空集合表示没有受保护内容权限；`available=false`、超限、超时或响应不完整时，服务三不查询 protected 索引（fail-closed）。服务一不得返回 `*` 通配符。

## 5. 批量检查候选结果

### 请求

```http
POST /internal/v1/authorization/check-batch
```

```json
{
  "subject_type": "user",
  "subject_id": "user_001",
  "organization_id": "org_001",
  "snapshot_id": "snap_001",
  "checks": [
    {
      "check_id": "c1",
      "resource_type": "knowledge_item",
      "resource_part": "display",
      "resource_id": "ki_001",
      "action": "view"
    },
    {
      "check_id": "c2",
      "resource_type": "attachment",
      "resource_part": "content",
      "resource_id": "att_001",
      "action": "view"
    }
  ]
}
```

允许的组合：

```text
knowledge_item + display   → view/download
knowledge_item + original  → view（当前 model.fga 没有 original.download）
attachment     + metadata  → view
attachment     + content   → view/download
```

因此本期服务三只请求 `knowledge_original.view`；“敏感原文下载”不能在不修改当前模型的前提下单独表达，服务一应拒绝该组合或由业务层另行处理，不能错误地把 `view` 当作 `download`。

服务一将逻辑资源映射到当前 FGA 对象，并逐项执行 Check。响应必须与请求 `checks` 一一对应且保持顺序：

```json
{
  "snapshot_id": "snap_001",
  "decisions": [
    {"check_id": "c1", "allowed": true},
    {"check_id": "c2", "allowed": false}
  ]
}
```

服务三只保留 `allowed=true` 的候选。缺少决策、数量不一致、服务一异常或权限检查超时，相关结果全部按拒绝处理。

## 6. 服务三调用时序

### 全局搜索和 AI 文档

```text
规范化查询
→ 并行：Embedding + search-scope（仅 original/content）
→ display 索引召回
→ protected 索引带 auth_object_key 过滤召回
→ 合并候选
→ check-batch(view)
→ RRF/去重/可选重排
→ AI 入口再次 check-batch(view)
```

### 知识库搜索

```text
知识库范围过滤
→ display 索引 BM25
→ protected 内容按同一 search-scope 规则处理
→ check-batch(view)
→ 返回高亮结果
```

受保护内容必须在 ES BM25/kNN 查询前过滤，不能先全量召回再依赖事后权限过滤。公开内容不需要放入 `search-scope`。

## 7. 关系同步边界

服务二负责根据消息、群聊、附件和审批结果调用服务一的关系写入/撤销接口；服务三不调用这些写接口。

服务一需要保证：

- 群聊成员和安全管理员的用户关系可同步为 `viewer/downloader`；
- 组织其他成员默认没有受保护内容权限；
- 审批通过后只给指定用户写入对应的 `viewer` 或 `downloader`；
- 成员退出、授权过期或资源删除时撤销旧关系；
- 关系同步成功后递增并返回 `acl_version`。

## 8. 错误和缓存

| 情况 | 服务一响应 | 服务三处理 |
|---|---:|---|
| 参数或资源组合非法 | 400 | 请求失败 |
| 服务 Token 无效 | 401/403 | 请求失败 |
| OpenFGA 暂不可用 | 503 | 受保护结果为空 |
| 授权范围超过上限 | 413 | 受保护结果为空 |
| 没有授权对象 | 200，`objects` 为空 | 不查询 protected |
| Check 结果缺失 | 200 但数量不一致 | 全部拒绝 |

`search-scope` 可按 `subject_id + organization_id + resource_parts + knowledge_base_id` 短时缓存；缓存必须有 TTL，并在 ACL 版本或快照过期时失效。缓存不能替代 `check-batch`，也不能用于放宽权限。

## 9. 对接验收

- 服务一不需要新增 OpenFGA 类型或修改现有模型；
- 服务三只收到受保护对象键，不收到公开对象列表、正文或 OpenFGA DSL；
- 受保护 ES 查询始终包含非空 `auth_object_key` 过滤；
- display 和 protected 候选均可通过一次 `check-batch` 批量复核；
- 服务一异常时不会返回未过滤的受保护内容；
- `knowledge_original` 的查看权限和附件正文的查看/下载权限可分别验证。
