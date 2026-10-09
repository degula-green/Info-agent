# 树形RAG 审核吞吐指标 交付文档

- **阶段**: Phase 2 补齐（Week 7 Day 3-5「审核埋点」——批准率/拒绝率部分）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 批准率、拒绝率与审核吞吐完成；**审核时长未做**（见第 5 节）
- **关联文档**: 树形RAG实施计划.md (v2.5 §4.3 Week 7)、树形RAG-Phase2-窗口挂载与质量体系-交付文档.md、树形RAG-指标暴露与口径修正-交付文档.md

## 1. 起点：审核数据有，但看不见

`entity_review_requests` 从 Phase 1 起就在记录每一次审核动作（action / reviewer_id /
candidate_id / 时间戳），可指标层一直只有"待审候选数"，没有任何吞吐或质量口径：

```text
/metrics 原有关联序列：rag_tree_pending_candidate_count
缺：审核了多少、批准率多少、拒绝率多少
```

结果就是决策点 3 里的"人工审核量可承受"没有任何数字可依，计划 4.2.3 要求的
"审核吞吐"面板也没有数据源。

## 2. 交付物：从既有记录推出，不加新表

`tree_metrics` 新增三个字段（PG 与 InMemory 两个实现同步）：

```text
review_actions         {"promote": n, "merge": n, "ignore": n, "defer": n}
review_count           审核动作总数
review_approval_rate   已裁决审核中的批准占比
```

**口径（这是本次唯一需要判断的地方）**：

```text
批准 = promote + merge
拒绝 = ignore
defer（"稍后处理"）既不算批准也不算拒绝，两边都不进
approval_rate = 批准 / (批准 + 拒绝)
```

`defer` 必须排除，因为它表达的是"还没决定"，不是"否"。把它算成拒绝的话，审核人越
谨慎（越常点稍后），批准率越难看，指标会把正确的行为报成问题。

`merge` 算批准：合并到既有实体同样意味着"这个候选该存在"，只是归属不同。

### 新增 Prometheus 序列

```text
rag_tree_review_count{scope="..."}
rag_tree_review_approval_rate{scope="..."}
```

分桶明细（各 action 的数量）保留在带鉴权的 `tree-metrics` JSON 里，不铺成时间序列——
按 action 铺标签会让序列数按 scope × action 膨胀，而批准率与总数已经够看趋势。

## 3. 为什么现在做，而不是等有审核了再说

审核数据是**只在审核发生的那一刻**产生的。口径现在定下来，第一批真实审核进来时就能
直接读数；等有人开始审了再补，前面那批就没有可对齐的口径（尤其是 defer 怎么算，
事后回填要重新解释历史数据）。

当前 `entity_review_requests` 是 0 行，所以指标读数是 0——这不是缺陷，是冷启动的
正常状态，和 `mount_coverage` 为 0 是同一类情况。

## 4. 验收证据

```text
pytest -q   ->  204 passed, 4 skipped
```

真实库（PG）两个 scope 各跑一次 `tree_metrics`，新 SQL 不报错：

```text
user 40a975eb         | review_count = 0 | approval_rate = 0.0 | actions = {} | pending = 88
organization 61d4401e | review_count = 0 | approval_rate = 0.0 | actions = {} | pending = 5
```

新增用例：defer 不计入拒绝（1 批准 / 1 拒绝 / 1 稍后 → 0.5，而不是 0.333）、无审核时
不除零、merge 计为批准、snapshot 透传、flatten 输出两个新序列。

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| **审核时长** | 这一项没做。审核时长是"审核人从打开候选到点下决定花了多久"，只有前端知道，必须由审核页上报停留时长并在 `entity_review_requests` 上加列（一次迁移）。当前没有真实审核可校准字段语义，先不做半成品 |
| 可替代的代理指标 | `entity_candidates.first_seen_at` → 审核记录 `created_at` 的差值可以在不加列的情况下给出**排队等待时长**，它回答的是"审核积压多久"，与"审核人花多久"是两件事。需要的话可以单独加，口径要写清楚 |
| 拒绝率单独序列 | 未单列。`review_actions` 里有原始分桶，`1 - approval_rate` 也能算，再铺一条序列属于重复 |

## 6. 变更文件

```text
services/rag/app/infrastructure/persistence/mvp.py  审核动作聚合 + 批准率口径（PG/InMemory）
services/rag/app/application/tree_metrics_service.py 两个新序列与 HELP
services/rag/tests/test_tree_metrics_service.py      +4 例，扩展 flatten 用例
services/rag/docs/树形RAG实施计划.md                 拆出已完成与未完成两行
```
