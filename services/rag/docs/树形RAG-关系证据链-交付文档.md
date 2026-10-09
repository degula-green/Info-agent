# 树形RAG 关系证据链 交付文档

- **阶段**: Phase 3 补齐（Week 10 Day 1-2「关系质量与证据」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 证据链端到端可读；关系**准确率**标注仍待人工（见第 5 节）
- **关联文档**: 树形RAG实施计划.md (v2.5 §5.3)、树形RAG-Phase3-关系网络-交付文档.md

## 1. 起点：证据写进去了，读不出来

关系的写入路径一直是带证据的：`window_scan_service` 每看到一个窗口就把该窗口的 chunk
id 传进 `upsert_entity_relation`，SQL 冲突时把新旧证据**并集**保存（不覆盖）。设计上
这是为了回答"这条边是模型偶尔提了一次，还是多个窗口都同意"。

但读取路径把它整个丢掉了：

```text
find_related_entities 的 SELECT 只取 e.id / domain / canonical_name /
  relation_type / confidence / source / target
  —— 没有 r.evidence_chunk_ids
```

后果是这条信息**只进不出**：管理端 `GET /entities/{id}/relations` 和检索诊断都拿到
不到证据，口头上的"有关系"在系统里对不上账。两个实现（PG 与 InMemory）缺的是同一处。

## 2. 交付物

### 2.1 仓储层返回证据

```text
PG        SELECT 增加 r.evidence_chunk_ids
InMemory  {"evidence_chunk_ids": [...]}
```

两个实现返回同一个键，调用方不需要区分后端。结果结构与之前兼容——只是多一个字段。

### 2.2 两处消费方

| 消费方 | 拿到什么 | 为什么 |
|---|---|---|
| 管理端 `GET /entities/{id}/relations` | **完整**证据链 | 研发排查"为什么系统认为张三参与了这个项目"时要能看到全部支撑 chunk |
| 检索诊断 `diagnostics.related_entities` | 最多 **5** 条 | 检索诊断每次查询都会写进 `search_history`，证据链随窗口累积会无限增长；截断后的列表仍能回答"是不是模型孤证" |

截断这件事是刻意的：诊断字段是每查询落库的，不设界就会让 `search_history` 随证据
增长而膨胀。完整链走管理端接口按需取，不放进每条检索记录。

## 3. 验收证据

```text
pytest -q   ->  204 passed, 4 skipped
```

PG 端到端往返（临时作用域，跑完即删）：

```text
建两个实体 + 一条带证据的关系（evidence_chunk_ids = ['0'*64]）
find_related_entities(direction=inbound) -> 1 条
evidence: ['0000000000000000000000000000000000000000000000000000000000000000']
清理后 (relations, entities) = (0, 0)
```

新增断言：检索诊断里的 `related_entities[0].evidence_chunk_ids` 等于写入的那条、
仓储查询同样带出证据。

## 4. 口径补充：证据链长度是"共识度"的代理

关系置信度由模型给出，不会因为多看到几次而变高；但**证据条数**会随窗口累积增长。
两者合起来才是完整判断：

```text
confidence 高 + 证据 1 条    -> 模型很确定，但只有一个窗口这么说
confidence 中 + 证据 5 条    -> 模型不太确定，但多个窗口反复看到
```

这也是为什么证据必须能被读出来，而不是只用来做冲突去重。低置信度的边要不要从
"阈值丢弃"改成"保留但降权"，等有真实关系数据后再定——现在 `entity_relations` 是
0 行，没有样本可依（见第 5 节）。

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| 关系准确率标注 | Phase 3 验收标准里的「关系准确率 > 80%」需要人工标注集，属人工投入项，尚未开始 |
| 低置信度关系降权 | 当前仍是查询侧 `min_confidence` 阈值丢弃，没有"保留但带权参与排序"。计划文档写明是"若后续发现低置信边仍有价值"再做的条件项；现在 `entity_relations = 0`，没有样本支撑这个判断，不提前实现 |
| 多跳与防环 | 深度固定 1 跳，1 跳不可能成环；扩到 2 跳时要同时引入访问集合与数量上限 |
| 真实关系数据 | 关系要两端都能落到注册表实体才会写入，而 `entity_registry` 当前为 0（候选未审核），所以证据链在真实环境里还没有数据可看 |

## 6. 变更文件

```text
services/rag/app/infrastructure/persistence/mvp.py  两个实现返回 evidence_chunk_ids
services/rag/app/application/rag_service.py         检索诊断带上证据（有界 5 条）
services/rag/tests/test_relation_expansion.py       +2 断言
services/rag/docs/树形RAG实施计划.md                 勾选 Week 10 Day 1-2
```
