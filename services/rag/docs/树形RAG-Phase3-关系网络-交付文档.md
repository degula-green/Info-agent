# 树形RAG Phase 3 交付文档：关系网络

- **阶段**: Phase 3 / 关系网络
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-10
- **状态**: 关系扩展检索与关系查询接口完成；关系质量标注待做（见第 4 节）
- **关联文档**: 树形RAG实施计划.md (v2.4)、树形RAG-Phase2-窗口挂载与质量体系-交付文档.md

## 1. 阶段目标

让关系数据在检索侧真正生效，支持"横向查找"类查询，例如"张三参与了哪些项目"。

## 2. 交付物

### 2.1 关系查询（仓储层）

`find_related_entities(scope, entity_ids, relation_types, direction, min_confidence, limit)`

```text
FUNCTION find_related_entities(scope_type, scope_id, entity_ids, relation_types, direction, min_confidence, limit)
  1. 以入参实体为种子，在 entity_relations 上做 1 跳查询
     - direction=outbound  种子作为 source，取 target
     - direction=inbound   种子作为 target，取 source
     - direction=both      两者都取，返回边另一端
  2. 过滤：scope、confidence >= 阈值、关系类型（可选）、邻居必须 active
  3. 按 confidence 降序、名称排序，取 limit
```

**深度固定为 1 跳**：设计文档允许 1-2 跳，但每多一跳候选集就成倍放大。这里只做
一跳，是否继续展开由调用方决定，避免无界图遍历。

**方向不能靠猜**：边是 `张三 --participates_in--> A项目`。从 A项目 看它是
inbound，从张三 看它是 outbound。两个方向的查询结果不同，因此接口显式暴露
`direction`，并且 SQL 里 `ON` 子句出现在 `WHERE` 之前，参数顺序按 SQL 文本排列
（这一点在实现时踩过一次坑：`both` 模式下的邻居占位符必须排在匹配占位符之前）。

### 2.2 关系扩展检索（`RAGRetrievalService._expand_relations`）

```text
触发条件（全部满足）:
  - tree_mode == "tree"
  - RAG_TREE_RELATION_EXPANSION_ENABLED（默认 true）
  - 已定位到实体，且节点内检索结果数 < top_k * 0.5

动作:
  1. find_related_entities(min_confidence=0.7, limit=3)
  2. 用邻居实体再做一次限定范围的 BM25 + kNN
  3. 结果标记 source["retrieval_origin"] = "related"
  4. 以 RAG_TREE_RELATION_WEIGHT（默认 0.3）参与 RRF，权重低于实体自身节点

降级:
  - 关系查询失败 -> degraded += relation_lookup_failed，退回全局检索
  - 关系范围检索失败 -> degraded += relation_search_failed，退回全局检索
```

为什么不直接退回全库：定位已经知道用户说的是哪个实体，直接全库等于把这份信息
扔掉；一跳扩展保留了它，同时还能覆盖"张三参与的项目"这类横向问题。

### 2.3 关系查询接口

```text
GET /api/v1/admin/entities/{entity_id}/relations
    ?scope_type=organization
    &relation_type=participates_in      # 可选
    &direction=both|outbound|inbound    # 默认 both
    &min_confidence=0.7                 # 默认 0
    &limit=20

Response: { entity_id, direction, count, items[{entity_id, domain,
            canonical_name, relation_type, confidence,
            source_entity_id, target_entity_id}] }
```

与其它管理接口共用同一套 Core 能力校验。

### 2.4 新增配置

```text
RAG_TREE_RELATION_EXPANSION_ENABLED   默认 true
RAG_TREE_RELATION_MIN_CONFIDENCE      默认 0.7
RAG_TREE_RELATION_MAX_ENTITIES        默认 3
RAG_TREE_RELATION_WEIGHT              默认 0.3
```

## 3. 验收证据

### 3.1 单元测试

```text
pytest: 124 passed, 4 skipped

新增 test_relation_expansion.py（4 例）:
  - 节点偏薄时触发一跳扩展，结果标记 retrieval_origin=related
  - 节点结果达到 top_k*0.5 时不扩展（默认 top_k=8，四条即达标）
  - 只有 tree 模式扩展，shadow 不扩展
  - 查询方法的方向 / 置信度阈值 / 关系类型过滤
```

### 3.2 真实接口验证

在真实组织 scope 建 `A项目`、`张三` 两个实体与一条
`张三 --participates_in--> A项目`（confidence 0.9），然后查询：

```text
GET /entities/{A项目}/relations?direction=both
  -> count=1，返回 张三 与 participates_in，source/target 正确

GET /entities/{A项目}/relations?direction=outbound
  -> count=0（边的方向是 张三 -> A项目，项目侧没有出边）

GET /entities/{张三}/relations?direction=outbound
  -> count=1，first=A项目 type=participates_in
```

三个方向的结果互不相同的这一组对照，正是方向语义正确的证据。验证后测试数据已清理。

## 4. 未完成项

### 4.1 关系提取准确率标注

实施计划 5.4 要求"关系提取准确率 > 80%（人工抽样）"。当前只有结构化的校验
（枚举、自环、空端过滤）与置信度合并，没有人工标注的准确率数字。

### 4.2 合并实体的关系端点修正

实施计划提到"合并实体时同步修正关系端点"。当前 merge 语义是**把候选名称加成
目标实体的别名**，并不创建被合并的实体行，因此没有需要改写的边。
`merged_into_entity_id` 那条路径目前未被使用；一旦启用删除式合并，需要补
端点改写的迁移与测试。

### 4.3 低置信度关系的降权策略

当前用阈值丢弃（查询侧 `min_confidence`），没有实现"保留但降权"。若后续发现
低置信边仍有价值，需要把 `confidence` 带进图遍历的排序，而不是简单过滤。

### 4.4 多跳与防环

只做 1 跳，没有环检测——因为深度为 1 时不可能成环。扩展到 2 跳时需要同时引入
访问集合与数量上限。

## 5. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-10-10 | Phase 3 关系扩展检索与关系查询接口完成 |
