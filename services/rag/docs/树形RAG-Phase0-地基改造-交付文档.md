# 树形RAG Phase 0 交付文档：地基改造

- **阶段**: Phase 0 / 地基改造
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **关联文档**: 树形RAG实施计划.md (v2.4)、实体定位五层管线接口草案.md

## 1. 阶段目标

把现有表结构改造成新设计要求的形态，清空树侧派生数据并重建，让 Phase 1
可以直接在正确的地基上开发，不背旧逻辑。

## 2. 交付物

### 2.1 数据库迁移

| 文件 | 说明 |
|---|---|
| `db/migrations/20261009_tree_rag_v2_schema.up.sql` | 正向迁移 |
| `db/migrations/20261009_tree_rag_v2_schema.down.sql` | 回滚（仅结构） |

已接入两处部署清单：

- `docker/docker-compose.yml` → `knowledge-migrate` 的 psql 序列与 volume 挂载
- `docker/docker-compose.server.yml` → `knowledge-migrate` 的迁移循环

迁移内容：

```text
0. 启用扩展：vector（pgvector）、pg_trgm（gin_trgm_ops 依赖）
1. entity_registry：domain CHECK 收窄到 5 类；新增 keywords / description /
   embedding vector(1536) / embedding_status / chunk_count / last_mentioned_at
   等字段与 HNSW、trgm、GIN 索引
2. entity_candidates：domain CHECK 收窄；entity_aliases：新增 domain CHECK
3. chunk_branches：删除 match_method / match_score，新增 confidence /
   mount_method；删除 branch_key 与 tree_nodes 外键；主键改为 (chunk_id, entity_id)
4. 删除 tree_nodes
5. 新建 entity_relations（带 scope 字段、关系枚举、唯一约束）
```

顺序上有一处必须遵守：`branch_key` 是旧主键的成员，**必须先删主键再删列**；
`CREATE EXTENSION` 必须排在所有 `vector` 列之前。

### 2.2 数据处置

迁移前实测（`info-agent` 库）：

| 表 | 行数 | 处置 |
|---|---|---|
| `chunks` | 1662 | 保留 |
| `outbox_events` | 3166 | 保留 |
| `tree_nodes` | 45 | 随迁移删除 |
| `entity_candidates` | 35 | 清空（全为 `new`，无人工审核记录） |
| `entity_candidate_mentions` | 73 | 清空 |
| `entity_registry` / `entity_aliases` / `chunk_branches` / `entity_review_requests` / `branch_refresh_jobs` | 0 | 已为空 |

结论：无需要保留的人工审核成果，未执行种子导出。

### 2.3 ES mapping

`services/rag/config/elasticsearch/rag-chunks-index-v1.json` 新增：

```json
"entity_ids": { "type": "keyword" },
"entity_mounts": {
  "type": "nested",
  "properties": {
    "entity_id": {"type": "keyword"}, "domain": {"type": "keyword"},
    "confidence": {"type": "float"}, "method": {"type": "keyword"}
  }
}
```

已 PUT 到 `rag_chunks_display_v1` 与 `rag_chunks_protected_v1`，均为 additive
变更，无需重建索引。`branch_keys` 字段保留在 mapping 中但代码已停止读写。

### 2.4 代码适配（旧字段 → 新结构）

迁移是破坏性的，应用代码必须同步切换，否则接口会直接 500。本次改动：

| 模块 | 改动 |
|---|---|
| `app/domain/rag.py` | `Chunk.branch_keys` → `entity_ids` + `entity_mounts`；`BranchMatch` → `EntityMount`（含 `es_document()`） |
| `app/application/entity_service.py` | `match_chunk_branches` → `match_chunk_mounts`，不再把时间编进挂载键 |
| `app/infrastructure/persistence/mvp.py` | `replace_chunk_branches` → `replace_chunk_mounts`（upsert + 置信度取大 + 来源强度优先）；删除建树逻辑；`get_tree` 改为从 registry + mounts 派生 |
| `app/application/index_service.py`、`branch_refresh_service.py` | 调用新方法；写回 ES 的字段改为 `entity_ids` / `entity_mounts` |
| `app/application/rag_service.py` | `_resolve_branches` → `_resolve_entities`，产出 entity_id；时间过滤回到 occurred_after/before 元数据 |
| `app/infrastructure/rag_elasticsearch.py` | 定位过滤由 `branch_keys` 前缀改为 `entity_ids` terms；bulk 容忍 `document_missing_exception` |
| `app/config.py` | `tree_branch_weight` / `tree_max_branches` / `tree_max_branch_keys_per_chunk` → `tree_mount_weight` / `tree_max_entities` / `tree_max_mounts_per_chunk` |

## 3. 验收证据

### 3.1 迁移正向与回滚（独立测试库）

在 `tree_rag_phase0_test` 库中先铺基线 schema，再跑正向与回滚，对比结构快照：

```text
baseline tables/cols/cons/idx = 17 / 310 / 135 / 104
UP       tables/cols/cons/idx = 17 / 313 / 131 / 105
DOWN     tables/cols/cons/idx = 17 / 310 / 135 / 103
round-trip tables identical      : True
round-trip columns identical     : True
round-trip constraints identical : True
round-trip indexes identical     : True
```

### 3.2 脏数据清理

在测试库插入 `domain='asset'` 的实体、别名与候选后执行迁移：

```text
迁移前 entity_registry: [('asset', 1), ('project', 1)]
迁移后 entity_registry: [('project', 1)]
迁移后 aliases: 0（随实体级联删除）
迁移后 candidates: []
CHECK 生效：asset 已被拒绝
```

### 3.3 实库应用结果

```text
表数 = 17 | tree_nodes 存在 = False | entity_relations 存在 = True
chunks = 1662（保留）
entity_registry / entity_aliases / entity_candidates / chunk_branches / entity_relations = 0
扩展: pg_trgm, vector(0.8.5)
```

### 3.4 挂载写入路径（真实数据端到端）

在真实 user scope 建两个实体（`青云飞鹏`、`服务器`）并执行刷新：

```text
changed = 12  耗时 = 9.8s
mounts 总数 = 12（mount_method=explicit, confidence=1.0, status=active）
ES 命中 = 7 / 12（其余 5 个 chunk 本就不在 ES 索引中，1662 vs 1314）
ES 文档 entity_ids 已正确写入
```

验证后已清理：实体与挂载归零，ES 上 7 篇文档的 `entity_ids` / `entity_mounts` 已清空。

### 3.5 接口与测试

```text
GET /api/v1/admin/entity-tree?scope_type=user          -> OK
GET /api/v1/admin/entity-tree?scope_type=organization  -> OK（修复前 500）
GET /api/v1/admin/entity-candidates?scope_type=organization -> OK

pytest: 82 passed, 4 skipped
ES mapping: entity_ids=keyword, entity_mounts=nested（两个索引一致）
```

## 4. 遗留与下一阶段入口

以下问题已确认存在，但不阻塞 Phase 0 验收，留待后续阶段处理：

1. **实体被删除时 ES 会残留挂载标记**：删除实体走级联删除挂载行，刷新任务
   看到的挂载集与库内一致（都为空）而跳过，不会回写 ES。需要在删除路径显式
   入队一次强制刷新，或让刷新按"曾经有过挂载"识别需要清理的 chunk。
2. **挂载刷新仍是全量扫描**：本次已优化为"无挂载的 chunk 直接跳过"，1603 个
   chunk 的刷新从 214s 降到 9.8s；但实体数量增长后仍需按候选实体反查 chunk，
   而不是遍历全 scope。
3. **PG 与 ES 的文档数差异（1662 vs 1314）**：属迁移前既有状态，需要确认这些
   chunk 是历史版本还是漏索引。
4. **Phase 1 入口**：`tree_mode` 仍为 `off / shadow / boost` 三态，尚未切换为
   `off / shadow / tree`；五层定位管线（L0-L5）与 pgvector 实体向量回填尚未实现。

## 5. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-10-09 | Phase 0 完成：迁移、数据处置、ES mapping、代码适配与验收 |
