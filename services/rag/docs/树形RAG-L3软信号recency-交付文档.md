# 树形RAG L3 软信号（recency） 交付文档

- **阶段**: Phase 1 补齐（Week 3 Day 3-4「软信号加权」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: recency 端到端生效；**keywords 软信号仍未实现**（见第 5 节）
- **关联文档**: 树形RAG实施计划.md (v2.5 §3.2.1)、实体定位五层管线接口草案.md、树形RAG-Phase1-定位管线与检索接入-交付文档.md

## 1. 起点：三个软信号只实现了一个

接口草案把 L3 的软信号写得很具体：

```text
type_hint 与 domain 一致          距离加权，如 distance * 0.9
last_mentioned_at 在近 3 个月内    距离加权，如 distance * 0.95
keywords 命中 mention             距离加权，如 distance * 0.97
```

而 `EntityLocator._rank` 只做了 type，**注释里却写着"Type and recency only
reorder"**——注释比实现多了一个信号。

`last_mentioned_at` 的情况更彻底：Phase 0 迁移就把这一列加进了
`entity_registry`，但全仓没有一处写它、也没有一处读它（扫过 358 个列名，它属于
仅有的 4 个未被引用的列之一）。也就是说这个信号从迁移那天起就是摆设。

## 2. 交付物

### 2.1 写入：挂载就是"被提到"

`last_mentioned_at` 只在一个地方维护——两条挂载路径共用的那个漏斗
`_upsert_mount_rows`（显式挂载走 `replace_chunk_mounts`，窗口扫描走
`merge_chunk_mounts`，都汇到这里）：

```sql
UPDATE entity_registry
   SET last_mentioned_at = CURRENT_TIMESTAMP
 WHERE id = ANY(%s::uuid[])
   AND (last_mentioned_at IS NULL OR last_mentioned_at < CURRENT_TIMESTAMP)
```

只前进不后退（`<` 判断），所以补写历史挂载不会把时间戳往回拨。

**刻意不在 `upsert_entity` 里写。**"最后被提到"应当来自内容，而不是来自注册表被编辑：
改名、合并同样会走 `upsert_entity`，如果那里也写，一个只是被重命名的老实体就会显得
"刚刚被提到"。代价是刚 promote、还没挂载的实体时间戳为 NULL，没有 recency 加成——
这恰好是正确语义。

### 2.2 读取：候选把它带到排序

三个定位查询（L1 exact / L2 fuzzy / L3 semantic）在自己的 SELECT 里带上
`e.last_mentioned_at`，两个仓储实现同步；`EntityCandidate` 新增
`last_mentioned_at` 字段（带默认值，不影响既有构造）。

### 2.3 排序：soft，不是 filter

```text
近 90 天内提到过  ->  score * 1.03（上限 1.0）
```

两个刻意的取舍：

1. **永不过滤**。草案与计划都写明"只有 scope 是硬过滤，type / recency / keywords
   只做排序加权"。老但正确的实体必须仍然可达，recency 只能影响谁排前面。
2. **强度弱于 type**。草案里 type 是 `distance * 0.9`、recency 是 `distance * 0.95`，
   代码里 type 用的是 `score * 1.05`（相似度空间的既有写法），recency 取 `* 1.03`
   以保持"type 更强"的相对关系。

## 3. 实际影响

```text
候选 A  old project   0.80  120 天前提到过   -> 0.80
候选 B  new project   0.79    3 天前提到过   -> 0.8137   ← 胜出
```

0.79 与 0.80 这种近似平局正是 recency 该起作用的场合（"上周聊过的那个项目"）；
差距大的时候它翻不过来，也不会把候选删掉。

## 4. 验收证据

```text
pytest -q   ->  221 passed, 4 skipped
```

新增用例：

```text
3 个月窗口边界      89 天内算近期、91 天不算、None 不算近期
重排且不删候选      0.79(新) 越过 0.80(旧)，两个候选都还在，分数为 0.8137
挂载即被提到        挂载前 last_mentioned_at 为 None，挂载后不为 None，实体仍可达
```

真实库只读校验（不写入）：

```text
locate_entities_exact / _fuzzy / _semantic  ->  三条新 SQL 均正常执行（该 scope 注册表为空，返回 []）
recency 更新语句（传入不存在的 uuid）        ->  语句合法，rowcount=0，未改动任何数据
```

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| **keywords 软信号** | 没做。`keywords` 这一列被**读**（用于拼接实体 embedding 文本、注册表加载），但没有任何地方**写**它——promote 路径的 `upsert_entity` 根本不接受 keywords 参数。要让它生效，得先有产出关键词的环节（窗口抽取输出里加字段并落库），那是模型侧改动，不是排序侧改动 |
| 前端/审核页展示 | 审核页目前不显示"最近被提到"。列已经有值，要展示是前端小改动，等有真实实体后再说 |
| recency 强度 | 1.03 是按草案相对关系取的，没有线上样本可校准。等首批实体与查询进来后，可结合标注集评估再调 |

## 6. 变更文件

```text
services/rag/app/domain/location.py                     EntityCandidate 增加 last_mentioned_at
services/rag/app/application/entity_locator.py          窗口判定 + 排序加权
services/rag/app/infrastructure/persistence/mvp.py      写入（挂载漏斗）+ 三个查询读取（PG/InMemory）
services/rag/tests/test_entity_locator.py               +3 例
services/rag/docs/树形RAG实施计划.md                     标注软信号的实现边界
```
