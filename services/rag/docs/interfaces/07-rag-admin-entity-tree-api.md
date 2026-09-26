# 07 候选实体审核与树结构管理 API

> 状态：已冻结

## 1. 目标

提供组织管理员查看当前树结构和审核候选实体的接口。候选 Entity 不属于正式树，只有审核通过后才会创建或合并到正式 Entity。

## 2. 权限

```text
entity:read
entity:review
entity:merge
entity:admin
```

MVP 单组织模型下，Scope 由服务端根据当前用户生成。普通用户不能访问管理接口。

MVP 复用 Core 现有的组织管理员/信息管理员能力，不新增 `entity:*` OpenFGA 资源类型。RAG 在管理接口入口调用 Core 做组织级能力检查。

## 3. 组织树接口

### 获取组织树概览

```text
GET /api/v1/admin/entity-tree
```

Query：

```text
domain
entity_id
time_bucket
depth
include_stats=true
```

### Response

```json
{
  "scope_key": "organization:org-uuid",
  "registry_version": 12,
  "nodes": [
    {
      "node_id": "uuid",
      "node_type": "domain",
      "domain": "project",
      "node_key": "domain:project",
      "branch_key": "domain:project",
      "parent_id": null,
      "statistics": {
        "entity_count": 18,
        "chunk_count": 420
      }
    },
    {
      "node_id": "uuid",
      "node_type": "entity",
      "domain": "project",
      "entity_id": "uuid",
      "canonical_name": "青云飞鹏官网项目",
      "node_key": "entity:project:uuid",
      "branch_key": "entity:project:uuid",
      "parent_id": "uuid",
      "statistics": {
        "time_bucket_count": 3,
        "chunk_count": 86
      }
    }
  ],
  "next_cursor": null
}
```

树接口不返回：

```text
Chunk 正文
protected 原文
Embedding
大体积 Chunk ID 数组
```

### 获取节点详情

```text
GET /api/v1/admin/entity-tree/nodes/{node_id}
```

返回：

```text
node_id
node_type
domain
entity_id
canonical_name
time_bucket
branch_key
registry_version
statistics
children
```

## 4. 候选实体列表

```text
GET /api/v1/admin/entity-candidates
```

Query：

```text
status
domain
query
min_score
page
page_size
```

Response：

```json
{
  "items": [
    {
      "candidate_id": "uuid",
      "candidate_name": "青云官网项目",
      "candidate_domain": "project",
      "normalized_key": "青云官网项目",
      "score": 0.82,
      "status": "review_ready",
      "mention_count": 6,
      "distinct_chunk_count": 5,
      "distinct_source_count": 3,
      "distinct_conversation_count": 2,
      "suggested_entity_id": null,
      "first_seen_at": "2026-09-20T10:00:00Z",
      "last_seen_at": "2026-09-27T10:00:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

## 5. 候选实体详情

```text
GET /api/v1/admin/entity-candidates/{candidate_id}
```

返回候选聚合和脱敏 mentions：

```json
{
  "candidate_id": "uuid",
  "candidate_name": "青云官网项目",
  "candidate_domain": "project",
  "status": "review_ready",
  "score": 0.82,
  "suggested_entity_id": null,
  "resolved_entity_id": null,
  "mentions": [
    {
      "mention_id": "uuid",
      "chunk_id": "sha256",
      "surface_form": "青云官网项目",
      "context_excerpt": "青云官网项目计划在下月上线",
      "confidence": 0.86,
      "extraction_method": "llm",
      "created_at": "2026-09-27T10:00:00Z"
    }
  ]
}
```

`mentions` 只允许返回 display 或脱敏上下文。

## 6. 审核候选

```text
POST /api/v1/admin/entity-candidates/{candidate_id}/review
```

Request：

```json
{
  "review_request_id": "uuid",
  "action": "promote",
  "target_entity_id": null,
  "canonical_name": "青云飞鹏官网项目",
  "domain": "project",
  "note": "确认是官网项目",
  "expected_status": "review_ready"
}
```

同时支持 Header：

```text
Idempotency-Key: <uuid>
```

动作：

```text
promote 创建正式 Entity
merge   合并到已有 Entity
ignore  拒绝并忽略
defer   暂缓
```

规则：

- `merge` 必须提供 `target_entity_id`。
- `promote` 不能复用现有正式 Entity 的 normalized key。
- `review_request_id` 必填，同一候选的同一请求 ID 只能执行一次。
- 所有动作必须校验候选当前状态，避免并发审核覆盖。
- 审核成功后增加 `registry_version`。
- 合并或创建后创建 `branch_refresh_jobs`。

Response：

```json
{
  "candidate_id": "uuid",
  "status": "promoted",
  "resolved_entity_id": "uuid",
  "registry_version": 13,
  "branch_refresh_job_id": "uuid"
}
```

## 7. 正式 Entity 管理（MVP 可选增强）

MVP 必须支持查看正式 Entity 和查看 Entity 详情。

直接修改规范名、Domain、状态以及单独增删 Alias 属于后续增强项。MVP 中正式 Entity 和 Alias 的主要来源是候选审核的 `promote` 和 `merge` 动作。

```text
GET    /api/v1/admin/entities
GET    /api/v1/admin/entities/{entity_id}
PATCH  /api/v1/admin/entities/{entity_id}
POST   /api/v1/admin/entities/{entity_id}/aliases
DELETE /api/v1/admin/entities/{entity_id}/aliases/{alias_id}
```

PATCH 只允许：

```text
canonical_name
domain
status
```

禁止直接修改 `entity_id`。

## 8. Branch 刷新和验收

审核成功后返回 `branch_refresh_job_id`，管理员可以查询：

```text
GET /api/v1/admin/branch-refresh-jobs/{job_id}
```

验收：

1. 候选不进入正式树。
2. 列表展示候选名称、Domain、评分、状态、来源统计。
3. 详情只展示脱敏证据。
4. `promote` 创建正式 Entity 和 Branch。
5. `merge` 只添加别名并保留正式 Entity ID。
6. `ignore` 不创建 Entity。
7. 审核后增量刷新 Branch Key，不重建全部 Chunk。
8. 组织树只展示节点和统计，不展示 protected 正文。
