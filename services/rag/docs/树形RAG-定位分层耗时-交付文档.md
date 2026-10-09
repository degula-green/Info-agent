# 树形RAG 定位分层耗时 交付文档

- **阶段**: Phase 1 验收补口（决策点 2 的「常规路径 p95 ≤ 80ms」）+ Phase 2 看板数据源（计划 4.2.3「定位分层延迟」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 分层计时与测量脚本完成并实测；**80ms 目标在当前部署下未达标**，原因见第 4 节
- **关联文档**: 树形RAG实施计划.md (v2.4)、树形RAG-Phase1-定位管线与检索接入-交付文档.md、树形RAG-指标暴露与口径修正-交付文档.md

## 1. 要解决的问题

实施计划把「实体定位 p95 ≤ 80ms」写成 Phase 1 的准入条件（决策点 2），
Phase 1 交付文档里也留了一句"需要真实查询集才能验证"。核实后发现比这更早一步的
问题：**定位管线根本没有计时**——`EntityLocator` 只记录每层命中了几个候选，
没有任何耗时数字，这条验收线连"测不出来"都算不上。

## 2. 交付物

### 2.1 分层计时（`app/application/entity_locator.py`）

```text
L0  注册表读取 + mention 抽取（二者都是候选查找之前的纯进程内工作，合并计一档）
L1  精确匹配（canonical_name / alias）
L2  模糊匹配（trigram / 编辑距离 / 包含）
L3  语义匹配（embedding 调用 + 向量检索）
L4  LLM 上下文验证
L5  汇总（scope 组装、残查询改写、截断）
```

每层在 `layer_trace` 里带自己的 `elapsed_ms`，`locate()` 的诊断新增：

```json
{
  "locate_ms": 1011.3,
  "layer_ms": {"L0": 205.2, "L1": 137.4, "L2": 212.0, "L3": 470.0, "L5": 0.06}
}
```

定位是**按 mention** 跑的，一条两实体的查询会做两次 L1。计划里的预算是按查询算的，
所以 `layer_ms` 把同层耗时**求和**而不是取平均（`_layer_totals`）。

### 2.2 出到检索诊断与指标

| 位置 | 改动 |
|---|---|
| `rag_service.py` | 把 `locate_ms` / `locate_layer_ms` 提到诊断顶层，随 `record_search` 落库 |
| `mvp.py` | `list_search_diagnostics` 投影新增 `locate_ms`（PG 与 InMemory 两个实现） |
| `tree_metrics_service.py` | 新增 `locate_latency_ms` / `locate_escalated_ms`（p50 / p95 / max / sample_count） |
| `/metrics` | 新增 `rag_tree_search_locate_p50_ms`、`_p95_ms`、`_sample_count`、`_escalated_p95_ms` |

**L4 单列不进常规分位**：计划第 635 行明确要求"升级路径延迟单独统计，不并入常规
p95"。一次 LLM 往返会直接定义整个数字，所以 `llm_invoked` 的行进
`locate_escalated_ms`。

### 2.3 测量脚本

```text
python scripts/measure_locate_latency.py --scope-type user --scope-id <uuid> --limit 20
python scripts/measure_locate_latency.py --scope-type organization --scope-id <uuid> \
    --queries-file queries.txt --repeat 3 --json out.json
```

查询缺省取该 scope 最近的 `search_history.query_redacted`——`redact_query` 只掩掉长
数字串和邮箱，取回来的仍是可用的真实查询。定位在进程内跑同一份 `EntityLocator`，
不回写任何数据。分位数用与服务完全相同的最近秩约定，两个来源的数字可以直接对比。

## 3. 实测（2026-10-09，本地服务 + 远端中间件）

```text
scope        user:40a975eb-41f6-44e6-be62-788554aa6f21
queries      12 条真实历史查询 x 1 = 12 runs
escalated    0 (L4)

layer       p50 ms    p95 ms    max ms
L0           205.2     361.7     361.7
L1           137.4     168.0     168.0
L2           212.0     307.3     307.3
L3           470.0     601.5     601.5
L5             0.0       0.1       0.1
total       1011.3    1329.8    1329.8

gate  常规路径定位 p95 <= 80ms  ->  未达标（p95 = 1329.8ms，样本 12）
```

同一天另跑 8 次单发请求（HTTP 走完整 `/search/tree`），形状一致：L0 ~220ms、
L1 ~150ms、L2 ~220ms、L3 ~510ms，总耗时中位数 ~1.17s，其中一次 L3 抖动到 4.56s
（embedding API 单次变慢，不是代码问题）。

## 4. 结论：80ms 在当前部署下不可达

把耗时按层摊开看，答案很直接：**这些时间不是算出来的，是等出来的。**

```text
L0 = 575ms（首测）/ 205ms（稳定）  一次 entity_registry 读取
L1 = 137ms                          一次 pg_trgm / 精确查询
L2 = 212ms                          一次模糊查询
L3 = 470ms                          一次 embedding HTTP + 一次向量查询
```

单个数据库往返就是 100~300ms 量级，因为 PostgreSQL（`123.57.175.182:5432`）不在
本机。定位的"常规路径"至少包含 **L0 + L1 两次往返**，光这两项就已经 ~340ms，
是 80ms 的四倍多。也就是说：

- 现在这个数字衡量的是**网络带宽/机房距离**，不是定位算法的效率。
- 想让这条验收线有意义，先要让 L0 和 L1 的依赖靠近服务：把 PG 放到同机房（或至少
  同可用区），或把注册表（实体 + 别名）按 scope 常驻进程内、只做增量刷新。
- 在中间件仍在远端的前提下，任何一次检索都不可能低于 ~300ms，端到端
  「p95 ≤ 150ms」（计划 3.4）同样不可达。

这一条要写进决策点 2 的复核：**验收标准本身需要跟着部署形态修订**，或者按"进程内
计算耗时"重新定义口径（把 IO 等待单独列出）。当前实现已经把两者分开计量，
两种口径都能算。

## 5. 验收证据

```text
pytest -q                                              ->  155 passed, 4 skipped
真实 /search/tree（POST, x-user-id=40a975eb-…）         ->  200
  diagnostics.locate_ms        = 1674.506
  diagnostics.locate_layer_ms  = {"L0":575.178,"L1":160.262,"L2":268.166,
                                   "L3":670.827,"L5":0.062}
  diagnostics.effective_execution_path = tree_shadow
measure_locate_latency.py（12 条真实查询）              ->  见第 3 节表格
```

新增用例：分层计时每层都有数值且 L0 不超过总耗时、多 mention 时同层耗时按查询求和、
`locate_ms` / `locate_layer_ms` 从检索诊断透出、L4 升级样本不进常规分位、
`flatten_metrics` 输出新增的四个序列。

## 6. 未做与后续

| 项 | 说明 |
|---|---|
| L4 样本为 0 | 当前未接 verifier，`allow_llm` 路径不触发，因此 `locate_escalated_ms` 暂时恒为 0 |
| 80ms 口径 | 需要按第 4 节结论与部署形态一起定：要么拉近中间件，要么改成"进程内耗时"口径 |
| 常规路径查询集 | 脚本已能取真实历史查询；要做正式验收仍建议人工挑一批覆盖精确匹配 / 别名 / 语义 / 无命中的查询 |

## 7. 变更文件

```text
services/rag/app/application/entity_locator.py     分层计时 + locate_ms / layer_ms
services/rag/app/application/rag_service.py        诊断顶层透出定位耗时
services/rag/app/application/tree_metrics_service.py 定位分位（L4 单列）+ 指标
services/rag/app/infrastructure/persistence/mvp.py  投影 locate_ms
services/rag/scripts/measure_locate_latency.py      分层耗时测量脚本（新增）
services/rag/tests/test_entity_locator.py           +2 例
services/rag/tests/test_mvp_retrieval.py            +2 断言
services/rag/tests/test_tree_metrics_service.py     +1 例，扩展 3 例
```
