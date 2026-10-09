# 树形RAG 审核时长 交付文档

- **阶段**: Phase 2 补齐（Week 7 Day 3-5「审核埋点」——最后一项：审核时长）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 端到端完成（迁移 + 接口 + 服务 + 仓储 + 前端上报 + 指标）
- **关联文档**: 树形RAG实施计划.md (v2.5 §4.3 Week 7)、树形RAG-审核吞吐指标-交付文档.md

## 1. 为什么这一项不能像批准率那样等

批准率/拒绝率可以从已有的 `entity_review_requests.action` 直接推出来，所以当时先交付了
那一半。审核时长不一样：**它只在审核发生的那一刻可观测，而且只有审核页知道**——
后端拿不到"审核人盯着这个候选看了多久"。

这意味着字段不存在的话，第一批真实审核的时长会**永久丢失**。而且它的语义不需要样本
才能定义：从抽屉打开到提交决定。所以这次不等"有审核记录再说"。

## 2. 交付物

### 2.1 迁移（`20261013_tree_rag_review_duration`）

```sql
ALTER TABLE rag_mvp.entity_review_requests
    ADD COLUMN IF NOT EXISTS review_duration_ms INTEGER;

CHECK (review_duration_ms IS NULL
       OR (review_duration_ms >= 0 AND review_duration_ms <= 86400000))
```

约束用 `DO $$ ... IF NOT EXISTS (pg_constraint) ...` 包了一层，保证重复执行不报错。
已接入两个部署清单：`docker-compose.yml` 的 psql 序列与卷挂载、`docker-compose.server.yml`
的迁移循环。

### 2.2 上报链路

```text
审核页   抽屉打开记 openedAt，提交时 durationMs = Date.now() - openedAt
API      CandidateReviewBody.duration_ms, 可选, 0..86_400_000
服务层   _review_duration_ms() 再夹一次（客户端是唯一来源，属不可信输入）
仓储     PG 写入 review_duration_ms；InMemory 存在 review 记录里
```

**批量审核不上报。** 那条路径从列表直接提交，没有"停留"过程；写成 0 会污染平均值，
所以留 NULL。

### 2.3 指标

```text
tree-metrics JSON     review_duration_avg_ms / review_duration_sample_count
Prometheus            rag_tree_review_duration_avg_seconds    （按基数单位用秒）
                      rag_tree_review_duration_sample_count
```

## 3. 三个刻意的语义选择

| 选择 | 理由 |
|---|---|
| **NULL ≠ 0** | "没上报"和"花了 0 毫秒"是两件事。平均值只对非 NULL 求，并把样本数一起暴露——否则样本 1 条的"平均 3 秒"和样本 300 条的看起来一样可信 |
| **越界夹紧而不是拒绝** | 客户端是唯一来源，时钟偏移或坏集成不该让一次真实审核失败。API 模型、服务层、仓储各夹一次（服务层是主闸门，仓储那层是 CHECK 前的最后防线） |
| **Prometheus 用秒** | 存储用毫秒（前端本来就算毫秒），暴露按基数单位换算成秒，符合 Prometheus 惯例 |

## 4. 验收证据

```text
pytest -q                     ->  226 passed, 4 skipped
vue-tsc --noEmit              ->  无输出（类型检查通过）
npm test（前端）               ->  72 passed
```

迁移正向 / 回滚 / 再正向（真实库，该表当时 0 行）：

```text
before          column=False constraint=False rows=0
up              column=True  constraint=True  rows=0
down (rollback) column=False constraint=False rows=0
up again        column=True  constraint=True  rows=0
```

新 INSERT 与表结构的匹配用 `EXPLAIN`（只解析+计划，不写入）验证——9 列全部对上：

```text
Insert on entity_review_requests  (cost=0.00..0.01 rows=0 width=0)
```

新增用例：未上报保持 NULL、越界夹到 1 天、非数字丢弃而不抛、服务层把超大值夹紧后
再交给仓储、平均值忽略未上报的审核、flatten 输出两个新序列。

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| 只有平均值，没有百分位 | 要做 p50/p95 得把每次时长当样本（PG `percentile_cont` 或 Prometheus histogram）。先有数据再决定值不值得 |
| "停留时间"的口径边界 | 当前是"抽屉打开到提交"，中途切走再回来也算在内。若将来需要"实际专注时长"，得靠页面可见性事件（`visibilitychange`）细分，那是另一个语义 |
| 前端未展示 | 审核页不显示自己的用时；审核人看不到，也就没有"被计时"的压力。要展示是小改动，但涉及产品判断，没做 |
| 无真实数据 | `entity_review_requests` 仍为 0，指标读数 0 属冷启动正常状态 |

## 6. 变更文件

```text
db/migrations/20261013_tree_rag_review_duration.up.sql / .down.sql   新增
docker/docker-compose.yml                          迁移序列 + 卷挂载
docker/docker-compose.server.yml                   迁移循环
services/rag/app/routers/admin.py                  请求体 duration_ms
services/rag/app/application/entity_review_service.py  夹紧后透传
services/rag/app/infrastructure/persistence/mvp.py 写入 + 平均值聚合（PG/InMemory）
services/rag/app/application/tree_metrics_service.py   两个新序列
apps/web/src/api/rag.ts                            可选 duration_ms
apps/web/src/views/info/InfoEntityReviewPage.vue   抽屉打开→提交计时
services/rag/tests/test_review_duration.py         新增 4 例
services/rag/tests/test_tree_metrics_service.py    +1 例，扩展 flatten 用例
services/rag/docs/树形RAG实施计划.md                勾选该项
```
