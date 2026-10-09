# 树形RAG 挂载置信度阈值 交付文档

- **阶段**: Phase 2 补齐（Week 7「检索阈值参数」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 端到端生效并在真实索引上验证
- **关联文档**: 树形RAG实施计划.md (v2.5 §4.3 Week 7)、树形RAG-Phase1-定位管线与检索接入-交付文档.md §4.2

## 1. 起点：阈值一直在传，就是没人用

`min_mount_confidence` 从 Phase 1 起就贯通了契约：

```text
SearchRequest.min_mount_confidence
  -> LocateRequest.min_mount_confidence
  -> locate.scope.min_mount_confidence（未指定时由定位器给默认 0.65）
```

但 ES 侧只按 `entity_ids` 的平面 terms 过滤，**置信度从未参与筛选**。Phase 1
交付文档把它记在未完成项里，理由写得也对："当前所有挂载都是 explicit
（confidence=1.0），阈值要到 Phase 2 的 window_batch 挂载出现后才有区分意义"。

现在 window_batch 挂载带 0.65~0.85 的模型置信度，阈值有意义了。

## 2. 两个问题，第二个更隐蔽

### 2.1 平面列表表达不了"这个实体且置信度≥X"

`entity_ids` 是一个同步出来的平面 keyword 列表。它能回答"这个 chunk 挂到过 e1 吗"，
回答不了"挂到 e1 **且**置信度至少 0.85"。后者要问挂载文档本身，而 `entity_mounts`
在 mapping 里正是 nested 类型：

```json
{"nested": {"path": "entity_mounts", "query": {"bool": {"filter": [
    {"terms": {"entity_mounts.entity_id": ["e1", "e2"]}},
    {"range": {"entity_mounts.confidence": {"gte": 0.85}}}
]}}}}
```

只在**有阈值时**走 nested；没有阈值仍用原来的平面 terms，不给常规路径增加开销。

### 2.2 光加过滤还不够：调用方不传阈值时它根本不会生效

即使加了过滤，直接读 `request.min_mount_confidence` 也是错的——大多数调用方不传这个
字段，拿到的是 `None`，过滤条件不会被加上。而定位器其实**已经算出了有效阈值**
（请求值，否则默认 0.65），并且就放在 `locate.scope` 里。

所以检索服务现在把定位器算出的有效阈值回填到检索请求上，再交给索引层：

```python
if locate is not None and locate.scope.min_mount_confidence:
    retrieval_request = replace(
        retrieval_request, min_mount_confidence=locate.scope.min_mount_confidence
    )
```

不修这一步的话，功能看起来"实现了"，实际只在极少数显式传参的调用里生效。

## 3. 验收证据

```text
pytest -q   ->  229 passed, 4 skipped
```

**真实索引端到端**（写入一条挂载置信度 0.70 的文档，查完即删）：

```text
indexed: 1
threshold 0.65 -> matches: 1   （0.70 >= 0.65，命中）
threshold 0.85 -> matches: 0   （0.70 < 0.85，被筛掉）
deleted: 1
after cleanup -> matches at 0.65: 0
```

这条验证同时证明了两个层面：nested 查询被真实 mapping 接受（在非 nested 字段上做
nested 查询会直接报错），以及置信度比较的方向正确。

新增用例：无阈值时保持平面 terms、有阈值时切成 nested 且两个子句都在、平面列表被
替换而不是并存、以及**服务层把定位器的有效阈值（默认 0.65）真的送进了索引调用**。

## 4. 未做与后续

| 项 | 说明 |
|---|---|
| nested 查询成本 | nested 比平面 terms 贵，所以只在需要阈值时才用。若将来阈值变成默认必带，应重新评估查询成本 |
| 阈值取值 | 0.65 是定位器的默认，不是实测最优值。等有真实挂载与标注集后再校准（设计里提到严格场景用 0.85、探索场景用 0.65） |
| 未做置信度加权排序 | 本项只做"低于阈值就不要"，没有把 confidence 带进 RRF 权重。相邻的 Phase 3 未完成项（低置信关系降权）同理，等真实数据再定 |

## 5. 变更文件

```text
services/rag/app/infrastructure/rag_elasticsearch.py  阈值切 nested 过滤
services/rag/app/application/rag_service.py            回填定位器的有效阈值
services/rag/tests/test_mvp_retrieval.py               +3 例
services/rag/docs/树形RAG实施计划.md                    勾选「检索阈值参数」
```
