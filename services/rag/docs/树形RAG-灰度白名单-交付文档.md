# 树形RAG 灰度白名单与采样 交付文档

- **阶段**: Phase 1 补齐（Week 5 Day 3「灰度开关与用户白名单」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 白名单与影子采样完成并实测
- **关联文档**: 树形RAG实施计划.md (v2.5 §3.5)、树形RAG-Phase1-定位管线与检索接入-交付文档.md、树形RAG-指标暴露与口径修正-交付文档.md

## 1. 起点：灰度只有半个开关，采样是死的

计划 3.5 的灰度策略是按范围逐级放量（1 个用户 → 3 个 → 10 个），但代码里只有
一个全局 `RAG_TREE_MODE`：改它等于**所有人一起切**，没有"先把某一个组织放进
tree、其余留在 shadow"的手段。计划里那句"灰度优先按查询类型切分"也没有对应实现。

顺带又发现一个死配置：`RAG_TREE_SHADOW_SAMPLE_RATE` 只出现在 `config.py`，
文档和代码都不引用它——和之前的 `RAG_METRICS_ENABLED` 一样，声明了但从来没人读。

## 2. 交付物

### 2.1 模式解析（`app/application/tree_rollout.py`）

```text
RAG_TREE_MODE            部署默认模式（off / shadow / tree）
RAG_TREE_ROLLOUT_SCOPES  提升名单，格式 `user:<id>,organization:<id>`
```

优先级（`resolve_tree_mode`）：

| 默认模式 | 该 scope 在白名单 | 生效模式 | 说明 |
|---|---|---|---|
| off | 任意 | **off** | `off` 是硬杀开关，白名单不越权 |
| tree | 任意 | tree | 全局已放量，无需名单 |
| shadow | 是 | **tree** | 逐级放量的那一步 |
| shadow | 否 | shadow | 保持观测，不影响结果 |

`off` 必须压过白名单，否则回滚预案就作废了：计划 3.6 写的是"1. tree_mode 切回
shadow；2. 仍异常则切 off"。如果白名单优先，把 `RAG_TREE_MODE` 切成 `off` 之后，
恰好是那批最需要停下来的 scope 还在跑 tree 模式。

名单解析对噪声宽容但绝不猜：空项、重复项丢弃，缺少 `:` 的条目**忽略而不是补全**，
因为"写错导致谁都没放量"和"还没开始放量"看起来完全一样。

### 2.2 影子采样（把死配置接上）

shadow 模式要为每个查询跑一次定位（实测约 1 秒，见定位分层耗时文档），全量
shadow 在生产上是实打实的成本。`RAG_TREE_SHADOW_SAMPLE_RATE` 现在真的生效：

```text
rate >= 1.0  -> 全部采样
rate <= 0.0  -> 全部跳过
0 < rate < 1 -> sha256(scope + query) 取前 64 位映射到 [0,1)，小于 rate 才采样
```

用 hash 而不是随机数，是为了让**同一个查询重试时结果一致**——否则同一条查询的两次
记录一次采样一次跳过，诊断数据自相矛盾。用 `sha256` 而不是 Python 内置 `hash()`，
是为了进程重启后仍然一致（内置 hash 带随机盐）。

### 2.3 诊断与指标口径

诊断新增三个字段：

```text
tree_mode             本次请求实际生效的模式
tree_mode_default     部署默认值（放量后仍可回溯"为什么这个 scope 是 tree"）
tree_shadow_sampled   本次 shadow 请求是否真的执行了定位
```

**指标诚实性**：被采样跳过的请求没有做任何定位工作，它的"未命中"是省钱的结果，
不是定位失败。因此 `summarize_search` 把它们排除在检索率与延迟分位之外，并单独
计数（`shadow_skipped_count` → `rag_tree_search_shadow_skipped_count`）。这和处理
`scope_export` 是同一个原则：**没做这件事的行，不能进这件事的比率。**

## 3. 实测

起一个临时实例（`RAG_TREE_MODE=shadow`、名单 `user:40a975eb-…`、
`RAG_TREE_SHADOW_SAMPLE_RATE=0.5`），打真实 `/search/tree`：

```text
--- 白名单内 ---
40a975eb    tree_mode=tree    default=shadow  sampled=True   path=traditional

--- 其余 scope（采样率 0.5）---
2c76676e    tree_mode=shadow  default=shadow  sampled=True   path=tree_shadow
2d29803b    tree_mode=shadow  default=shadow  sampled=True   path=tree_shadow
484fdd66    tree_mode=shadow  default=shadow  sampled=False  path=tree_shadow
53c91dac    tree_mode=shadow  default=shadow  sampled=False  path=tree_shadow
86bfa332    tree_mode=shadow  default=shadow  sampled=True   path=tree_shadow
bfbc0979    tree_mode=shadow  default=shadow  sampled=False  path=tree_shadow
```

白名单内的 scope 生效模式被提升为 tree，其余保持 shadow；采样率 0.5 下 6 个 scope
命中 3 个，符合预期。白名单那个 scope 的 `path=traditional` 是因为该 scope 注册表
为空、没有任何实体可定位——tree 模式在无实体时本来就会退化，不是白名单失效。

## 4. 验收证据

```text
pytest -q                          ->  179 passed, 4 skipped
临时实例 8010 + 真实 /search/tree   ->  见第 3 节
```

新增用例：白名单内的 scope 提升为 tree、名单外保持默认、`off` 压过白名单、
同 id 不同 scope_type 不串（`user:o-1` ≠ `organization:o-1`）、非法默认值回落 off、
名单解析忽略噪声、采样率 1/0 的两端、同一查询可重复、2000 次采样比例落在
0.20~0.30、被采样跳过的行不计入命中率与延迟但单独计数。

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| 按查询类型切分 | 计划 3.5 建议"只对含明确实体的查询启用 tree"。这一条**已由管线本身承担**：tree 模式下没有定位到实体时，`select_tree_channels` 直接返回 `traditional` + `no_entity_match`，等于自动只对含明确实体的查询生效，不需要额外开关 |
| 回滚预案演练 | 预案已成文（3.6），本阶段仍未做实际演练。回滚路径本身已可用：`RAG_TREE_MODE=off` 是硬杀开关，且不删实体 / mount / 向量数据 |
| 白名单的运维入口 | 当前通过环境变量配置，改名单需要重启进程。要做在线放量需要加管理接口，属 Phase 4 范畴 |

## 6. 变更文件

```text
services/rag/app/application/tree_rollout.py      模式解析与采样（新增）
services/rag/app/application/rag_service.py       接入 rollout 控制 + 诊断字段
services/rag/app/application/tree_metrics_service.py 采样跳过不计入检索率
services/rag/app/infrastructure/persistence/mvp.py 投影 tree_shadow_sampled
services/rag/app/config.py                        RAG_TREE_ROLLOUT_SCOPES
services/rag/tests/test_tree_rollout.py           单测（新增 11 例）
services/rag/tests/test_mvp_retrieval.py          +1 例端到端放量
services/rag/tests/test_tree_metrics_service.py   +1 例，扩展 1 例
```
