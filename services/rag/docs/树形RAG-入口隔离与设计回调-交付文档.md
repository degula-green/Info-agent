# 树形RAG 入口隔离与设计回调 交付文档

- **阶段**: 架构回调（取代 Phase 1 的通道融合设计）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 入口隔离完成并在真实环境验证；agent 的树工具与自主回退**不在本轮**
- **关联文档**: 树形RAG-Phase1-定位管线与检索接入-交付文档.md（其中的通道融合语义已被本文取代）、树形RAG实施计划.md 原则3

## 1. 为什么要回调

Phase 1 的实现把树做成了"tree-first + 全库兜底"的**融合**：

```python
effective = dict(scoped_branches)
for name, values in global_branches.items():
    effective.setdefault(name, values)   # 两套通道同时进 RRF，且等权 0.5
```

这段的原始注释是 *"the global ones stay in the mix as a low-weight backstop"*，
但两个通道的权重实测都是 `0.5`——所谓 backstop 与树通道同权。

而最初的设计意图是：

```text
传统 RAG = 基础检索，只做「树不可用时的降级」，不参与树的召回
树结构   = 范围收窄 + 节点间横向跳跃；最后一层落下去仍是同一套混合检索，只是范围变小
两者关系 = 互斥
```

融合导致两个后果，都是真实的：**全局搜索框的结果依赖树**（定位错就影响它），以及
**每次"传统检索"都要先跑一遍定位**（实测 `/search/global` 的 `locate_ms` 约 1.3s）。

## 2. 回调后的语义

通道策略由**入口**决定，不再由全局开关决定：

| 入口 | 策略 | 行为 |
|---|---|---|
| `/search/global`（搜索框） | `hybrid` | 全库 bm25 + knn；**不定位、不含树通道** |
| `/search/knowledge` | `hybrid` | 同上 |
| `/search/content`（agent 内容检索） | `hybrid` | 同上 |
| `/search/sources` | `hybrid` | 原样（列来源，不检索内容） |
| `/ai/documents*`（知识问答） | `hybrid` | 该功能已废弃，不再管 |
| `/search/tree` | `tree` | 定位 → 实体范围（+一跳横向跳跃）；**不含全库** |

树入口在三种 rollout 状态下的行为：

```text
tree_mode=tree    → 返回实体范围结果；空/失败则返回空 + reason，不替代为传统结果
tree_mode=shadow  → 照跑树并记录诊断，但返回传统结果（回滚/验证用）
tree_mode=off     → 返回空 + reason=tree_disabled
```

当前部署默认值已切换为 `tree`，所有组织默认可用；`shadow/off` 保留用于回滚。

**服务端不再做回退**：树表面拿不到结果时给出 `fallback_reason`
（`tree_disabled` / `no_entity_match` / `empty_scope` / `branch_failed` /
`shadow_not_sampled`），由调用方决定要不要再走一次传统检索。

## 3. 实现

```text
channel_policy_for(entry)              entry -> "tree" | "hybrid"
select_retrieval_channels(...)          取代 select_tree_channels
  ├ policy != tree  → 全库通道，path=traditional
  ├ shadow          → 全库通道，path=tree_shadow，reason 照记
  └ tree            → 范围通道；无则 {} + reason（不替代）
run_unscoped      = 非树入口 or shadow          → 决定是否发出全库查询
run_tree_channels = 树入口 and 有实体            → 决定是否发出范围查询
```

关键点：**tree 模式下全库查询根本不发出**（不只是结果被丢弃），所以既没有混入，也不
再付出那次查询与定位的开销。横向跳跃（一跳关系扩展）只在树表面内发生，不会借道全库。

## 4. 真实环境验证（本地 8000，默认 tree_mode=shadow）

```text
/search/global     policy=hybrid  path=traditional  locate_ms=0        results=0
/search/content    policy=hybrid  path=traditional  locate_ms=0        results=0
/search/tree       policy=tree    path=tree_shadow  locate_ms=1281.6   reason=no_entity_match
```

两个传统入口的 `locate_ms` 是 **0**——定位管线不再被搜索框触发（回调前约 1.3s）。
树入口照常定位并给出明确原因；当前注册表为空，所以是 `no_entity_match`。

`pytest -q` → **234 passed, 4 skipped**（重写了 9 处旧融合语义的断言，
新增"传统入口永不定位"与"树入口无命中不回退"两条专项用例）。

## 5. 这轮没做的

| 项 | 说明 |
|---|---|
| agent 的树 tool | 用户明确本轮先不加 agent 意图；`/search/tree` 这个工具面已经就绪，封装留到下一轮 |
| agent 自主回退 | 「树不理想就再调一次传统 RAG」是 agent 侧的编排，本轮不碰 |
| 过薄判定 | 按要求先只判"空/非空"。`scoped` 非空就返回，不做 `top_k/2` 的过薄回退 |
| 横向跳跃的触发条件 | 仍是"节点薄（< top_k/2）才扩展"。是否改成"总是横向跳跃"待定 |

## 6. 变更文件

```text
services/rag/app/application/rag_service.py   channel_policy_for + select_retrieval_channels + 入口分支
services/rag/tests/test_mvp_retrieval.py      旧融合断言重写 + 2 条专项用例
services/rag/tests/test_rollback_drill.py     演练按新语义重写
services/rag/tests/test_relation_expansion.py 改为走树入口
services/rag/docs/树形RAG实施计划.md           原则3 与状态索引同步
```
