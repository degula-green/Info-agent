# 树形RAG 回滚预案演练 交付文档

- **阶段**: Phase 1 收尾（Week 5 Day 3「回滚预案演练」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 演练完成；Phase 1 任务清单全部勾选（唯一未达标的验收项是 80ms 定位延迟，属部署形态问题）
- **关联文档**: 树形RAG实施计划.md (v2.5 §3.6)、树形RAG-Phase1-定位管线与检索接入-交付文档.md、树形RAG-灰度白名单-交付文档.md

## 1. 演练目标

计划 3.6 的回滚承诺有三条，演练就逐条验：

```text
1. tree_mode 切回 shadow  -> 保留诊断，不影响检索结果
2. 仍异常则切 off         -> 纯传统检索
3. 数据保留               -> 不删除实体、mount 与向量，随时可以再打开
```

## 2. 演练方式

分两层，因为两层各自只能证明一半：

| 层 | 证明什么 | 为什么需要它 |
|---|---|---|
| 自动化演练（`tests/test_rollback_drill.py`） | 结果等价性：有树数据时，切到 shadow/off 后返回的 chunk 与 tree 模式的差异符合预期 | 真实库当前 `entity_registry = 0`，没有树数据可回退，光看线上跑不出这个结论 |
| 真实环境演练 | 可操作性：在真实服务上切换 `RAG_TREE_MODE`，检索不中断、定位确实停掉、数据没被删 | 自动化用的是内存仓储，证明不了真实进程与配置生效 |

## 3. 自动化演练

用一个"带实体过滤才返回 scoped-chunk、否则返回 global-chunk"的索引器当对照，
逐条走完回滚步骤：

```text
步骤 0  tree     path=tree        结果={scoped-chunk, global-chunk}  locate_ms>0
步骤 1  shadow   path=tree_shadow 结果={global-chunk}                locate_ms>0
步骤 2  off      path=traditional 结果={global-chunk}                locate_ms=0
```

三条承诺对应的断言：

```text
shadow 与 off 的结果集合完全一致        -> 回滚不改调用方看到的内容
shadow 仍然做了定位（locate_ms>0）      -> 回滚第一步保留诊断能力
off 完全不定位（locate_ms=0、无实体）   -> 回滚第二步真的停下成本
回滚后实体仍 active                     -> 只停用，不清理
```

顺带确认了一个容易误解的点：**tree 模式是 tree-first 而不是 tree-only**。树内通道
与全局通道都进融合，全局作为兜底，所以 tree 模式下两个 chunk 都在；切到 shadow/off
之后剩下的正是全局那一个。这正是设计里"错误的实体匹配只该降低排序，不该藏掉答案"
的行为。

另外补了一条：采样率为 0 时，shadow 请求仍然落库并被标记
`tree_shadow_sampled=false`，而不是从记录里消失——最省钱的"停止 shadow"方式也不该
让流量统计出现空洞。

## 4. 真实环境演练

另起一个 `RAG_TREE_MODE=off` 的实例（8012），与线上的 shadow 实例（8000）打同一条查询：

```text
before (shadow)        status=200  mode=shadow  path=tree_shadow  locate_ms=1222.6  entities=0
rollback step 2 (off)  status=200  mode=off     path=traditional  locate_ms=0       entities=0
after (shadow back)    status=200  mode=shadow  path=tree_shadow  locate_ms=860.9   entities=0
```

三次都 200，`off` 下定位彻底停止（`locate_ms=0`、`path=traditional`），切回来立刻恢复。

数据保留核对（演练前后各查一次）：

```text
BEFORE {'entity_registry': 0, 'entity_aliases': 0, 'chunk_branches': 0, 'entity_relations': 0, 'entities_with_vector': 0}
AFTER  {'entity_registry': 0, 'entity_aliases': 0, 'chunk_branches': 0, 'entity_relations': 0, 'entities_with_vector': 0}
```

真实库当前树侧四张表都是空的（窗口扫描只产出了候选，还没有人工审核通过），所以这次
真实演练只能证明"开关可用、定位停得掉、数据不被动"，证明不了结果等价性——那部分由
第 3 节的自动化演练覆盖。chunk 结果为空是因为该 scope 在 ES 里没有命中数据，与回滚
无关。

## 5. 结论与遗留

- **回滚路径可用**：`RAG_TREE_MODE` 是硬杀开关，压过灰度白名单；切 shadow 保留诊断，
  切 off 停止全部定位开销；两步都不删数据。
- **Phase 1 任务清单至此全部勾选**。剩下的唯一未达标项是"常规路径定位 p95 ≤ 80ms"
  （实测 1330ms），根因是中间件在远端、单次 PG 往返 100~300ms，**不是回滚或管线
  本身的问题**，需要先定部署形态或改口径（见 树形RAG-定位分层耗时-交付文档.md 第 4 节）。
- 真实环境演练的局限已写明：树侧表为空，结果等价性靠自动化演练保证；等首批实体
  审核通过后，应重跑一次带真实树数据的线上演练。

## 6. 变更文件

```text
services/rag/tests/test_rollback_drill.py   回滚演练（新增 6 例）
services/rag/docs/树形RAG-回滚演练-交付文档.md
services/rag/docs/树形RAG实施计划.md         勾选回滚演练、更新交付状态索引
```
