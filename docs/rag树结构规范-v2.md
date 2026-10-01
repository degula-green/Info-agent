# RAG 记忆树结构规范 v2

> 状态：评审中
> 本规范取代 `rag表结构及es索引设计.md` 与 `rag-retrieval-design.md` 中关于树结构与检索主路径的部分。

---

## 1. 文档目的

定义 RAG 记忆树 v2 的结构契约：**每一层的维度、键、存什么、不存什么、能提供什么性质的剪枝**，以及树与 chunk 索引、证据链路的绑定方式。

解决的问题是：当前的树付了构建与维护成本，却既不能证明自己在过滤，也不能保证不丢答案。本文档给出的不是补丁清单，而是一次**职责反转**——树从"证据提供者"变成"分支谓词提供者"。

---

## 2. 现状链路

```
POST /api/v1/ai/documents
  └─ RagSearchService.answer
       ├─ plan_query                    → preferred_tree_type（entity | session）
       ├─ TreeSearchService.search      → ES 三层下钻(root→internal→leaf) + fact 检索
       │    ├─ ES: memory_*_nodes_read  → 逐层导航
       │    ├─ ES: memory_*_facts_read  → 叶子下取 fact
       │    ├─ PG: evidence_for_facts   → fact ⋈ chunk 外键取原文
       │    └─ ES: chunk 索引           → 同会话 ±N 分钟邻居扩展
       ├─ needs_document_evidence = not final_results   ← 互斥门
       └─ （仅当树为空时）HybridRetriever: BM25 + kNN + RRF + 可选 rerank
```

---

## 3. 问题诊断

每条都有代码证据，可逐条复核。

### 3.1 树是唯一召回来源，融合通道被物理关闭

[search_service.py:209](../services/rag/app/services/search_service.py#L209)

```python
needs_document_evidence = not final_results
```

树一旦产出任何 chunk，BM25 + kNN + RRF + rerank 整条路径被跳过。**AI 问答场景下多路召回、RRF、rerank 是死代码。**

### 3.2 树永不空手，所以 3.1 的门永远关闭

[memory_elasticsearch.py:127](../services/rag/app/infrastructure/memory_elasticsearch.py#L127)

```python
body = {"query": {"bool": {"should": [{"match": {field: {"query": query}}}],
        "minimum_should_match": 0, "filter": filters}}, ...}
```

`minimum_should_match: 0` 使该 bool 匹配任何文档；`knn` 仅在 `vector is not None` 时附加（[:128-129](../services/rag/app/infrastructure/memory_elasticsearch.py#L128-L129)）。**向量调用失败时查询退化为 match-all**，返回任意 top-k 且分数为 0。

### 3.3 无事实的行照样产出证据

[memory_elasticsearch.py:112-113](../services/rag/app/infrastructure/memory_elasticsearch.py#L112-L113)

```python
if not fact_hits:
    output.append({**common, "fact": None, "score": base_score})
```

时间过滤只作用在 fact 的 `observed_at` 上（[:91-95](../services/rag/app/infrastructure/memory_elasticsearch.py#L91-L95)），节点选择不带时间过滤（[:102-110](../services/rag/app/infrastructure/memory_elasticsearch.py#L102-L110)）。**日期窗一收窄 → fact 全被过滤 → 仍插入 `fact: None` 行 → 带叶子来源 → 进 prompt 并标 `rag_eligible: True`。** 即：问"某月的事"拿到的是该叶子下随意一条附件内容，无任何事实依据。

### 3.4 树的键不可重现

leaf_id = `_stable_uuid("node", tree_id, "leaf", group_key, topic_key)`（[repository.py:87](../services/rag/app/infrastructure/persistence/repository.py#L87)），而 `topic_key` 来自 `route.topic`（[repository.py:104](../services/rag/app/infrastructure/persistence/repository.py#L104)）——**LLM 输出**。entity 树的 internal 键 `phase` 同理（[:90](../services/rag/app/infrastructure/persistence/repository.py#L90)）。

后果：重建时 LLM 给出的 topic 可能不同 → 叶子漂移 → 旧叶子带陈旧 fact 变孤儿。**B+ 树的结构由键唯一决定，而这棵树的结构由模型采样决定。**

### 3.5 三层节点的"时间区间"是假的

[repository.py:89-91](../services/rag/app/infrastructure/persistence/repository.py#L89-L91)：root / internal / leaf 的 `time_start` 与 `time_end` **全部写成同一条消息的 `sent_at`**。internal（月桶）在语义上不是区间，因此"这个月发生了什么"无法回答，时间谓词也无法用于剪枝。

### 3.6 树无法约束 chunk 检索

chunk 索引 [documents-index.json](../services/rag/config/elasticsearch/documents-index.json) **没有任何树身份字段**（无 `tree_id` / `node_id` / `leaf_node_id` / `subject_key`）。文档里"树限制候选范围"的断言（[rag表结构及es索引设计.md:234](rag表结构及es索引设计.md#L234)）只在 node/fact 层成立，到 chunk 只能限到**文档粒度**。叶子无法约束向量检索，"叶子载值用于检索"在实现上没有落点。

### 3.7 逐父 top-k 是论文明确否掉的写法（不是分层下钻）

[memory_elasticsearch.py:100](../services/rag/app/infrastructure/memory_elasticsearch.py#L100)

```python
branch_k = max(1, min(3, request.top_k))
```

论文否掉的是**逐父 top-k（per-parent beam）**，不是分层下钻本身——MemForest 自己也沿树逐层下到 L0。区别在于候选集是全局预算还是各父亲自保留：

- 逐父保留 → 分支数相乘（MemForest 的 `browser.py` 注释明确记录：*"The old implementation kept top-beam per parent, giving beam^depth"*，其消融显示 beam 3→10 为 +7.5pp）；
- 全局预算 → 候选集不随深度膨胀，且不同叶子的 fact 在同一排序里直接竞争。

本项目的写法是前者：`branch_k = max(1, min(3, request.top_k))`。除成本外还有两个后果：root 摘要是聚合文本、与具体问题相关性低，**root 选错后下游无法纠正**（每个父的 top-3 已被锁死）。

成本：roots 上限 50（[search_service.py:164](../services/rag/app/services/search_service.py#L164)），每 root 展开 1 + 3 + 9 = 13 次 ES 请求 → 理论上界约 650 次串行请求。

> **实测修正（2026-09-24，82 条测试集）**：旧代码实际平均只有 **8.5 次** ES 往返、树耗时 326ms，因为真实语料里 session 根节点只命中约 1 个，扇出从未展开。所以"650 次"是上界而非现状，**"树太重"在实测数据上不成立**（树仅占检索延迟 15%，大头是查询向量化 47%）。该问题应记为**潜在风险**（语料增长或 deep 模式下才触发），而非当前性能瓶颈的成因。

分数也不可比：`base_score = root + group + leaf 的 _score`（[:108](../services/rag/app/infrastructure/memory_elasticsearch.py#L108)），再与 fact 分数相加（[:115](../services/rag/app/infrastructure/memory_elasticsearch.py#L115)）——不同索引的无界 BM25 分数与可能来自 kNN 的有界分数混加。

### 3.8 实体是摆设，根因不是别名缺失

`plan_query` 只产出 `preferred_tree_type`；`subject_key` 仅在传 `conversation_id` 时当过滤条件。**查询侧根本不存在"问题 → 实体"的链路**，所以实体树建得再准也无法剪枝。

同时 `memory_entities.aliases` / `merge_status` 已建表（[20260916_rag_memory_tree.sql:92-93](../db/migrations/20260916_rag_memory_tree.sql#L92-L93)）但写入与读取都没接；`normalize_key` 只有小写 + 压空格（[memory.py:13](../services/rag/app/domain/memory.py#L13)），「青云项目」与「青云项目组」是两个实体。

### 3.9 权限边界未被结构保证

- 文档要求"不同权限边界不得混入同一 Leaf"（[rag流程设计.md:109](rag流程设计.md#L109)、[服务三PG与ES存储结构设计.md:211](服务三PG与ES存储结构设计.md#L211)）；
- 但 leaf 键只由 `group_key + topic_key` 决定，**不含任何权限维度**；
- `memory_nodes` 表**没有任何权限列**（[repository.py:352-356](../services/rag/app/infrastructure/persistence/repository.py#L352-L356)）；
- [repository.py:399](../services/rag/app/infrastructure/persistence/repository.py#L399) 只写 `display_summary`，而 `_summaries()` 的 `collect()` 递归收集节点下**全部**后代事实（含 protected，[pipeline.py:77-83](../services/rag/app/application/memory/pipeline.py#L77-L83)）；
- 节点摘要随 `path` 返回给调用方（[memory_elasticsearch.py:139-140](../services/rag/app/infrastructure/memory_elasticsearch.py#L139-L140)）并进入 prompt。

**若存在混合可见性叶子，受保护内容的摘要会被写进 display 节点索引、被 display 分支搜到、并原样返回给无权用户。**

### 3.10 写路径与兜底路由

- 每条消息 **7 次 LLM 调用**：1 次事实抽取 + 6 次节点摘要（两棵树 × 3 层节点）。
- `dirty` 列只写不读（[:355](../services/rag/app/infrastructure/persistence/repository.py#L355) 置 TRUE、[:399](../services/rag/app/infrastructure/persistence/repository.py#L399) 置 FALSE），**没有任何 dirty-path 刷新逻辑**，每次写入重算整棵相关树。
- 无事实时凭空造实体路由（[pipeline.py:28-35](../services/rag/app/application/memory/pipeline.py#L28-L35)）：`subject = organization_id or owner_user_id or "company"`，`topic = file_name`，`entity_type` 硬编码 `"organization"`。**用文件名当主题造组织实体**，直接导致 `general` 节点泛滥、树被切碎。
- 写路径无错误隔离：`MemoryPipeline.process` 的 embedding 与抽取调用无 try/except，**任一次失败导致整棵树的记忆写入丢失**（注意 `EmbeddingClient.embed` 已内置分批 ≤8 与 LRU 缓存，[client.py:55](../services/rag/app/infrastructure/embedding/client.py#L55)——缺的是失败隔离，不是分批）。

### 3.11 文档层面

- **不存在树结构的正式规范。** 键约定只存在于代码里（`root` / `group:{key}` / `leaf:{group}:{topic}`），从未写入任何文档。
- `rag-retrieval-design.md` 描述的检索主路径（chunk 级 RRF）与现在的树优先实现**互相矛盾且未更新**；`database.md` 缺全部 memory 表。
- 仓库同时存在两套打脸的设计文档，是"新代码一直兼容旧写法"的根源之一。

---

## 4. 设计目标与验收标准

| # | 目标 | 验收方式 |
|---|---|---|
| 1 | **分桶召回基线** | 五桶（无日期关键词 / 带日期 / 会话 / 附件文档 / 精确匹配）各 10–20 条，报 Recall@k、MRR 与路径分布 |
| 2 | **关键场景端到端通过** | 某月某事 / 某人承诺过什么 / 查某条具体消息 / 查电话或身份证 |
| 3 | **性能与成本达标** | 树检索 ES 请求数从数百降到 O(层数)；查询期 LLM 调用数与 p95 延迟设上限 |
| 4 | **树可证明在过滤** | 同时报出"剪掉多少候选"与"被剪掉的部分里有没有 gold"（`pruning_ratio` + `recall_loss`） |

目标 4 是本文档的核心：**只有确定性键剪枝能同时给出这两个数**。启发式剪枝永远无法证明自己没丢答案——这正是当前树"没起到过滤作用"却无法证伪的原因。

---

## 5. 核心设计判断

### 5.1 职责反转

**树负责缩小检索范围（可证明），chunk 负责提供证据（可授权），RRF 负责融合。**

树从"召回主体"降为"分支谓词提供者"；事实与 fact→chunk 回源降为补充证据与解释通道。当前所有病症都源于树同时兼任了召回：互斥门之所以致命是因为树同时兼任召回；`branch_k` 之所以又贵又漏是因为它在用逐父 top-3 模拟一个本该是全局预算的剪枝。

### 5.2 确定性键负责剪枝，LLM 摘要负责导航

两者**可以共存于同一节点**，但分工不再混淆：

- **键**（会话 / 实体 / `YYYY-MM`）→ 确定地丢掉不相关分支，**零漏召、可证明**；
- **摘要**（root + internal 的 LLM 摘要 + 向量）→ 粗略地问"哪个月可能相关"，**有漏召、不可证明，但便宜好用**，退化为纯粹的候选生成器。

这样才能既保留语义导航能力，又让剪枝保证可被度量和验证。

### 5.3 结构必须由摄取元数据决定，不能由模型采样决定

任何进入**键**的字段必须确定性（来自 `sent_at`、`conversation_group_id`、实体规范名）。LLM 输出（`topic` / `phase` / 摘要）只能作为**载荷标签**，不得占据结构位置。

---

## 6. 树结构规范 v2

### 6.1 层级定义

| 层 | `dimension` | 确定性键 | 存什么 | 不存什么 | 剪枝性质 |
|---|---|---|---|---|---|
| L0 root | `scope` | `(knowledge_base_id, tree_type, subject_key)`：session = 会话群；entity = 实体规范名 | 键 + 子指针 + 摘要 + 向量 | — | 确定性（谓词精确匹配） |
| L1 internal | `time` | `YYYY-MM`（两棵树统一用月） | 键 + 子指针 + LLM 摘要 + 向量 + **真实时间区间** | 不挂事实 | 确定性（时间范围） |
| L2 leaf | `group` | `(root_key, YYYY-MM)` → 每分组单叶 | `chunk_ids` + `fact_ids` + 标签（`topic` / `phase` / `fact_type`）+ 描述符 | 不生成导航摘要 | 继承 L0 / L1 |

两棵树的形状：

```
Session Tree                          Entity Tree
 Root (会话群)                          Root (实体规范名)
  └─ Internal (YYYY-MM)                  └─ Internal (YYYY-MM)
      └─ Leaf (该月载荷)                     └─ Leaf (该月载荷)
```

两棵树**只有 root 维度不同**（会话 vs 实体），分区维度统一为月。这样：

- 分区键完全确定，可重现，重建不会漂移；
- "某实体在某月"与"某会话在某月"都是精确的键路径；
- 深度一致（3 层），便于统一的遍历与预算逻辑；
- `topic` / `phase` / `fact_type` 全部降为叶子标签，用于展示、排序与解释，**不参与结构**。

> 不使用 `phase` 作为 entity 树的分段键：`phase` 是 LLM 输出，当分段键会重演 `topic` 的叶子漂移问题。

### 6.2 与 v1 的差异

| 项 | v1 | v2 |
|---|---|---|
| internal 键（session） | `YYYY-MM` | `YYYY-MM`（不变） |
| internal 键（entity） | `fact.phase`（LLM） | `YYYY-MM` |
| leaf 键 | `(group_key, topic_key)`，topic 来自 LLM | `(root_key, YYYY-MM)`，单叶 |
| `topic` / `phase` | 结构位置（键） | 载荷标签 |
| `time_start` / `time_end` | 单条消息 `sent_at`（三层相同） | 真实覆盖区间（三层各异） |
| 节点维度声明 | 无（靠 `node_type` 猜） | 新增 `dimension` 列 |
| 摘要范围 | root + internal + leaf | root + internal |
| 摘要刷新 | 每次重算整棵相关树 | dirty-path 局部刷新 |
| fact 挂载 | session 与 entity 双挂 + `level=3` 事实层 | 叶子载荷 + 独立 ANN 索引 |
| 剪枝 | 每父 top-3，逐层下钻 | 分支谓词 + 全局预算 |
| chunk 树身份 | 无 | `leaf_refs`（多值 keyword 数组） |
| 查询侧实体 | 无 | 实体词典确定性匹配 + ANN 兜底 |

### 6.3 事实的定位

**事实是值，不是索引层。** v1 把 fact 投影写成 `node_type="fact"` / `level=3` / `parent_id=leaf`（[repository.py:122](../services/rag/app/infrastructure/persistence/repository.py#L122)），而 PG 的 CHECK 只允许 `root/internal/leaf`——ES 有 4 层、PG 只承认 3 层的分裂状态。

v2 中事实：

1. 作为**叶子载荷字段**（`fact_ids`）存在，不再是独立层；
2. 同时进**独立的事实 ANN 索引**，用于自底向上召回（query → fact ANN → 反投影到叶子），这是"无谓词时的语义导航"来源；
3. 通过 `memory_fact_chunks` 外键继续支撑 fact → chunk 原文的精确取回（**注意：这不是向量检索，是主键 JOIN**，[repository.py:403-428](../services/rag/app/infrastructure/persistence/repository.py#L403-L428)）。

### 6.4 时间区间必须真实

root 覆盖其下全部时间跨度；internal 覆盖该月；leaf 覆盖其载荷范围。这是时间谓词能剪枝的前提，也是修掉 §3.5 的必须项。

---

## 7. chunk 与树的绑定

### 7.1 `leaf_refs` 多值数组

**一个 chunk 可能承载多个实体的事实**（同一段落里提到 A 和 B），因此 chunk 上的树身份**不是单值**：

```json
"leaf_refs": [
  "session:{subject_key}:{YYYY-MM}",
  "entity:{subject_key}:{YYYY-MM}"
]
```

- 用 `terms` 查询做 **any-match** 过滤：命中任一所选分支即候选；
- 过滤只需 any-match 语义，**不需要 nested**；
- 复合键字符串让"树类型 + 主体 + 分区"三元组自解释，避免多字段交叉过滤的歧义。

落点：`ChunkRecord.as_es_source()`（由 [elasticsearch.py:46-60](../services/rag/app/infrastructure/elasticsearch.py#L46-L60) `index_chunks` 写入），同步 [documents-index.json](../services/rag/config/elasticsearch/documents-index.json) 映射。

### 7.2 与权限维度的关系

若 §3.9 的核查确认存在混合可见性叶子，则 `leaf_refs` 与叶子键都必须带 **visibility 维度**，且摘要必须按可见性分区生成（`display_summary` / `protected_summary` 分别生成、分别索引）。

**这不是一个可选优化，而是一个前置设计输入**——核查结论决定键的形状。

---

## 8. 检索契约 v2

### 8.1 新流程

```
查询
 ├─ 1. 谓词解析           时间范围 / 会话 / 实体 / 精确值      query_planner
 ├─ 2. 树解析为分支集合    leaf_refs 列表                     memory_elasticsearch
 ├─ 3. 分支内检索         chunk 索引，filter: leaf_refs       ┐
 ├─ 4. 全库检索           同参数，无分支过滤                    ├─→ 5. RRF 融合
 ├─ 6. 可选 rerank                                            ┘   （分支结果加权）
 └─ 7. 授权复查 → 组 prompt
```

### 8.2 三条原则

1. **树是过滤通道，不是证据通道。** 树命中不等于可以直接交给模型；证据始终是授权后的 chunk。
2. **剪枝必须附加而非排他（本轮）。** 分支结果加权，全库通道始终存在。识别到实体只影响权重与排序，不排他。**确认零漏召后，再对高置信命中切到排他剪枝。**
3. **两者都空则不做剪枝。** 词典与 ANN 都没命中实体 → 完全不施加实体过滤。宁可不过滤，不可错过滤。

### 8.3 成本控制

- 分层遍历改为**批量取层**：每层一次 `terms`（`parent_id`）查询，替代每父一次请求 → ES 请求数从 O(根数 × 分支数) 降到 O(层数)；
- 全局预算替代 `branch_k`：每层保留全局 top-N，而非每父 top-3；
- 所有候选进入 RRF，不再做跨索引分数相加（§3.7 的量纲问题随之消失）。

### 8.4 被删除的 v1 行为

| v1 行为 | 处理 |
|---|---|
| `needs_document_evidence` 互斥门 | 删除，代之以 §8.1 的并行 + 融合 |
| `minimum_should_match: 0` | 删除；向量不可用时**不进树**，而不是退化为 match-all |
| `fact: None` 行 | 删除；叶子要么有载荷要么不产出 |
| 每父 top-3 | 换成全局预算 |
| 非 AI 入口的复用面 | `FullTextRetriever` 不再靠"改 entry 绕过向量化"的副作用表达语义 |

---

## 9. 实体身份（三层阶梯）

目标：让实体**在过滤中起作用**，而不是摆设。

### 9.1 写入侧：确定性归并

建实体前依次：

1. `(kb, entity_type, normalized_key)` **直配** → 复用；
2. **aliases 命中** → 复用并追加别名；
3. **确定性包含规则**（同 `entity_type`、一个是另一个的子串、长度比在阈值内、共享核心词）→ 挂为别名，记 `merge_status='candidate'`。

- LLM 抽别名**可以**，因为别名是"候选"不是"键"；
- **`merge_status` 的正确用法是分级而非开关**：`active` = 已确认归并；`candidate` = 启发式挂靠（查询侧可用但低权重，可人工复核后升级）；
- 误并风险：把两个不同实体的内容混进同一棵树，查询时互相干扰。以 `candidate` 标记使其可回滚。

### 9.2 查询侧：实体词典（当前完全缺失的一环）

- 建**实体词典**（`canonical_name` + `aliases`，按 kb 分区，常驻内存，写入时刷新）；
- 查询先做**确定性词典匹配**（子串 / AC 自动机）→ 产出 `subject_key` 谓词 → 在 ES 层精确剪枝：**零 LLM、零网络往返、零漏召**；
- 可上报：命中几个实体、剪掉多少分支、被剪掉的里面有没有 gold。

### 9.3 兜底：向量 ANN（附加，绝不排他）

实体名在写入时嵌向量（一小批，成本极低）；查询侧 ANN 找候选实体。**必须 union，不替代**：词典命中 ∪ ANN 命中；两者都空 → 不施加实体剪枝。

### 9.4 顺带解决键值精确查找

第二层的词典匹配天然就是键值查找。电话 / 身份证 / 编号这类一对一关系，查询出现该模式时直接按 `(entity_type, normalized_key)` 或 `fact_versions.normalized_value` 精确命中。

**`normalized_value`、`dedupe_key`、`memory_entities.normalized_key` 三个字段已经存在且只写不读**（[memory.py](../services/rag/app/domain/memory.py) 的 `dedupe_key`、[20260916_rag_memory_tree.sql](../db/migrations/20260916_rag_memory_tree.sql) 的实体键与事实版本），正好是它的落点。原本描述的两个检索模式（实体筛选 + 键值精确查找）是同一套机制。

---

## 10. 摘要策略

- **范围**：只对 root 与 internal 生成 LLM 摘要；叶子不生成，只存事实集合 + 可直接拼接的描述符（确定、免费、可复现）。每条消息的 LLM 调用从 6 次降到按分组数量计。
- **刷新**：dirty-path 局部刷新——只重算受影响路径上的节点，跨树并行。`dirty` 列从"只写不读"变成真正的驱动字段。
- **权限分区**：按 `display_summary` / `protected_summary` 分别生成、分别索引，禁止共享未经审计的摘要（范围以 §7.2 核查结论为准）。

---

## 11. 迁移与重建

| 项 | 决定 |
|---|---|
| 方式 | 可重建：新建 v2 索引与映射，从 PG 已有数据重建回填，旧索引只读保留 |
| 数据源 | `rag.memory_chunks` + `memory_facts`，**不重跑解析与事实抽取** |
| 幂等 | 复用现有写入路径（`upsert_memory` / `update_node_summaries` / `index_chunks`），不另写一套写库逻辑 |

**重建不修复抽取遗漏**：从 PG 重建意味着沿用现有事实抽取结果，抽取时漏掉的 chunk 依然不在树里。这正是全库通道（§8.1 第 4 步）必须保留的硬理由。

---

## 12. 实施阶段

### 阶段 0：前置核查（结论改变设计输入）

1. **环境迁移状态**：`memory_node_sources` 表与 `observed_at` 列是否已应用。`docker/docker-compose.yml` 的 migrate 列表**不含** `20260919_rag_node_sources.sql` 与 `20260920_rag_tree_context.sql`，而代码依赖它们——若未应用，`sources_for_nodes` 会抛异常并被 [memory_search_service.py:191](../services/rag/app/services/memory_search_service.py#L191) 静默吞掉。**先确认再动手。**
2. **混合可见性叶子核查**：查 PG `memory_node_sources` ⋈ `memory_sources`，确认是否存在同一 `node_id` 同时关联 display 与 protected 来源。**结论决定键的形状**（§7.2）。
3. 记录可重建数据量（`memory_chunks` / `memory_facts` / `memory_trees` 行数），估算回填耗时。

### 阶段 1：测试集与干净基线（必须早于重构）

- 建分桶测试集，gold 标到 `knowledge_item_id` / `chunk_id`；
- 打分脚本用 `/search/global` 与 `/search/tree`（**不要用 `/ai/documents`**——它走 LLM 生成，答案对错混入过多变量）；
- 直接复用已有诊断字段：[search_service.py:235-243](../services/rag/app/services/search_service.py#L235-L243) 的 `retrieval_stage` / `fallback_level` / `fallback_reason` / `tree_candidate_count` / `chunk_candidate_count`；
- **在现有链路上跑出基线**（含当前 `fact: None` 污染与互斥门的影响），否则"提升"与"零漏召"都无法证明。

### 阶段 2：写入侧 v2

- [repository.py](../services/rag/app/infrastructure/persistence/repository.py) `_memory_plan` / `ensure_path`：确定性键、写真实时间区间、节点写 `dimension`；
- 实体归并（§9.1）+ `aliases` 落库；
- 移除兜底组织路由（[pipeline.py:28-35](../services/rag/app/application/memory/pipeline.py#L28-L35)）；
- 摘要范围与 dirty-path 刷新（§10）；
- `leaf_refs` 下沉（§7.1）；
- 写路径加**逐项错误隔离**（§3.10）；
- 新迁移：`dimension` 列、（如需）node 权限维度。

### 阶段 3：重建回填

新脚本按 v2 规则重算挂载、`leaf_refs`、时间区间与摘要。可参考 [scripts/tree_rag_demo.py](../services/rag/scripts/tree_rag_demo.py) 的运行方式。

### 阶段 4：检索侧 v2

- 重写 `search_tree` 为分支解析 + 批量取层 + 全局预算；
- 新增实体词典 / 链接器（§9.2）；
- `query_planner` 扩展为产出结构化谓词（时间 / 会话 / 实体 / 精确值）；
- `search_service` 删除互斥门，改为分支内 + 全库并行 + RRF 融合 + 分支加权；复用 [fusion.py](../services/rag/app/application/retrieval/fusion.py) 的 RRF、[hybrid_retriever.py](../services/rag/app/application/retrieval/hybrid_retriever.py) 的 `_common_filters` / `authorize_results`；
- `memory_search_service` 降级为分支解析 + 解释上下文提供者。

### 阶段 5：文档收敛

本文档落盘后，标注 `rag-retrieval-design.md` 已过时，补齐 `database.md` 缺失的 memory 表。

### 阶段 6（触发式）

测试集证明高置信命中零漏召后，对这部分查询切到排他剪枝。

---

## 13. 验证

### 13.1 会被重构打破、必须同步改写的现有断言（不要删除）

1. `test_tree_context_routing.py::test_tree_result_contains_direct_and_neighbor_chunks`
2. `test_memory_tree.py::test_fact_is_deduplicated_and_mounted_in_session_and_entity_trees`（断言每树恰 3 节点）
3. `test_memory_tree.py::test_es_projection_and_layered_tree_search`（断言 `path == ["root","internal","leaf"]`）
4. `test_memory_tree.py::test_pipeline_summarizes_embeds_and_indexes`（断言 stage 序列）
5. `test_memory_tree.py::test_document_without_fact_is_mounted_as_source`
6. `test_qa_flow.py::test_document_question_searches_only_tree_sources`（断言 `fallback_level==0`）
7. `test_tree_context_routing.py::test_default_entity_and_session_routing`
8. `test_retrieval.py::test_source_scope_is_a_mandatory_chunk_filter`

### 13.2 执行清单

- 全量单测（改写后）；
- 五桶测试集在 v2 上跑，报 Recall@k / MRR / 路径分布，与阶段 1 基线逐桶对比；
- **影子对比**：同一批查询分别以"开启分支剪枝"与"关闭分支剪枝"执行，报 `pruning_ratio` 与 `recall_loss`（被剪掉部分里的 gold 命中数）——这是目标 4 的证据；
- 端到端四场景；
- 性能与成本：每查询 ES 请求数、查询期 LLM 调用数、p95 延迟。

---

## 14. 风险与未决

| 风险 | 影响 | 处置 |
|---|---|---|
| 混合可见性叶子结论未出 | 若存在，键与过滤都须加 visibility，且构成实际泄露 | 阶段 0 优先核查 |
| 重建沿用现有抽取结果 | 抽取遗漏的 chunk 不在树里 | 全库通道必须保留 |
| 一个 chunk 属于多个实体 | `leaf_refs` 多值 → 分支内候选集可能显著放大 | 影子对比中量化 |
| `topic_key` / `phase_key` 列降级 | 列名不再反映用途，易误导 | 保留列 + 加注释，不做迁移 |
| 实体包含规则误并 | 两实体内容混入同一棵树 | `merge_status='candidate'` 可回滚；本轮只做标记不做复核界面 |
| 8 个现有断言失效 | 回归真空 | 改写为新契约下的断言，不删除 |
| `database.md` 缺 memory 表 | 文档与实现脱节 | 阶段 5 补齐 |

---

## 附录：既有资产复用清单

| 资产 | 位置 | 用途 |
|---|---|---|
| RRF 融合 | [fusion.py](../services/rag/app/application/retrieval/fusion.py) | 分支内 + 全库两路融合（已实现，当前是死代码） |
| 公共过滤构造 | [hybrid_retriever.py](../services/rag/app/application/retrieval/hybrid_retriever.py) `_common_filters` | 扩展为接受 `leaf_refs` 分支过滤 |
| 最终授权复查 | `hybrid_retriever.authorize_results` | 直接复用 |
| 并行检索 | [elasticsearch.py](../services/rag/app/infrastructure/elasticsearch.py) `parallel_search` | filters 已是参数，天然可接分支过滤 |
| 邻居上下文 | `search_context_chunks` | 本轮不做会话回填，留待下一轮 |
| 嵌入客户端 | [embedding/client.py](../services/rag/app/infrastructure/embedding/client.py) | 已内置分批 ≤8 + LRU 缓存 |
| 幂等写入路径 | `upsert_memory` / `update_node_summaries` / `index_chunks` | 重建回填复用，不另写写库逻辑 |
| 检索诊断字段 | [search_service.py:235-243](../services/rag/app/services/search_service.py#L235-L243) | 测试集打分与路径分布图的直接数据源 |
| 事实向量 | 抽取时已存 | 自底向上召回（query → fact ANN → 反投影叶子）的数据前提已具备 |
