# RAG 召回率问题

- 文档日期：2026-10-08
- 状态：问题排查与优化方案
- 适用范围：RAG 检索、Agent Knowledge 工具、全局知识问答

## 1. 问题现象

用户提问：

```text
帮我查找一下aims的服务器信息
```

当前返回的答案把多个项目的服务器信息混在一起，来源中只有一条与 `aims` 明确相关，其他来源主要描述青云官网或其他通用服务器部署信息。

这不是模型自行生成，而是检索阶段就已经把弱相关 chunk 放进了最终来源列表。

## 2. 用户预期

查询中的 `aims` 应该是强约束条件。理想结果应满足：

- 来源必须与 `aims` 项目相关。
- `服务器`、`部署`、`账号`、`RDS`、`Redis` 只作为次级业务词。
- 没有出现 `aims` 的通用服务器内容不能进入最终来源。
- 如果 `aims` 无法识别或没有匹配内容，应明确返回低置信或空结果，不能退化成“所有服务器相关结果”。

## 3. 当前检索链路

```text
用户问题
  -> 查询 embedding
  -> BM25 召回
  -> KNN 召回
  -> 实体分支检索
  -> RRF 融合
  -> 资源级去重
  -> 最终 top_k
  -> Agent 组装回答
```

当前主要问题集中在 BM25 查询构造、实体约束和最终相关性过滤。

## 4. 根因分析

### 4.1 BM25 使用 OR 查询

位置：

```text
services/rag/app/infrastructure/rag_elasticsearch.py
```

当前查询使用：

```python
"operator": "or"
```

查询：

```text
aims 服务器 信息
```

在 ES 中实际近似为：

```text
含 aims
OR 含 服务器
OR 含 信息
```

因此：

- 只包含“服务器”的内容会进入候选。
- 只包含“账号”“root”“RDS”“Redis”的内容也会进入候选。
- `aims` 没有成为必需命中条件。

这是本次召回混乱的最直接原因。

### 4.2 `aims` 没有进入正式 Entity Registry

数据库现状：

```text
rag_mvp.entity_registry
  没有 active 的 aims 实体

rag_mvp.entity_candidates
  candidate_name=aims项目
  candidate_domain=project
  status=new
  mention_count=1
```

`aims` 只是候选实体，尚未审核为正式实体，也没有 `aims`、`AIMS` 等别名。

因此查询时实体匹配器无法识别 `aims`，也就无法生成实体分支约束。

相关位置：

```text
services/rag/app/application/entity_service.py
services/rag/app/application/rag_service.py
```

### 4.3 Tree 模式没有真正约束结果

当前配置：

```text
RAG_TREE_MODE=shadow
```

`shadow` 模式会记录实体分支匹配，但不会把分支结果用于最终过滤或排序。即使 `aims` 被识别，当前配置也不会排除其他项目的全局结果。

### 4.4 内容检索没有应用严格分数门槛

Agent 的 `knowledge.search_content` 默认：

```text
min_answer_score=0
```

这意味着 RRF 结果不会因为绝对相关性不足而被丢弃。

QA 路径有：

```text
RAG_QA_VECTOR_MIN_SCORE=0.72
RAG_QA_BM25_MIN_SCORE=0
```

但内容检索和 Agent 回答链路没有等价的通用门槛。

### 4.5 Reranker 关闭

当前配置：

```text
RERANK_ENABLED=false
```

BM25 和 KNN 的候选直接经过 RRF，没有 cross-encoder 做二次语义排序。

对于：

```text
aims 服务器信息
```

通用服务器文档可能在 BM25 上获得较高基础分，而真正的 `aims` 服务器信息因为文本较短或关键词覆盖较少，排名反而靠后。

### 4.6 RRF 只处理相对排名

RRF 的设计目标是融合多个召回分支，不是判断绝对相关性。

一个 chunk 即使只是弱相关，只要进入 BM25 或 KNN 的 top_k，就可能获得 RRF 票数并进入最终结果。

当前召回规模较大：

```text
RAG_BM25_TOP_K=40
RAG_KNN_TOP_K=40
RAG_KNN_NUM_CANDIDATES=160
RAG_FINAL_TOP_K=8
```

候选窗口越大，通用“服务器”内容越容易挤进最终来源。

## 5. 问题影响

- 回答会混合不同项目的信息。
- 用户无法判断哪条来源属于目标项目。
- 可能导致密码、账号、部署信息等敏感内容跨项目混入回答。
- 后续 RAG 评测无法稳定区分项目。

## 6. 优化方案

### P0：实体词必须成为强约束

查询解析后，如果识别到实体或高置信候选实体，构造为：

```text
must:
  aims

should:
  服务器
  部署
  IP
  账号
  RDS
  Redis
```

不是：

```text
aims OR 服务器 OR 账号 OR RDS OR Redis
```

建议短查询先采用：

```text
multi_match.operator = and
```

或至少让检测到的实体词进入 `must`，其他业务词进入 `should`。

### P1：审核并补齐 Entity Registry

在 RAG 管理后台处理候选实体：

```text
aims项目
```

增加别名：

```text
aims
AIMS
aims项目
aims系统
```

审核完成后重新生成相关 chunk 的 Branch Key。

### P2：逐步启用实体过滤

建议路径：

```text
shadow
  -> boost
  -> strict_entity_filter
```

规则：

```text
高置信实体命中
  -> 优先只检索该实体分支
  -> 分支为空时再降级到全局检索并标记低置信

没有实体命中
  -> 使用普通 BM25 + KNN
```

不能对所有查询默认硬过滤，否则新实体和未登记项目会出现“完全查不到”。

### P3：增加内容检索门槛

为 `entry=content` 和 Agent 回答链路增加：

- 实体命中必须满足
- 至少一个业务词命中
- 向量最低分
- BM25 最小命中条件

RRF 分数不能直接作为绝对相似度使用。

### P4：启用 Reranker

目标链路：

```text
BM25/KNN 召回 40 条
  -> RRF 取前 12 条
  -> Reranker 重排
  -> 最终 top 5
```

首版如果暂时无法接入外部 Reranker，可先使用规则重排：

```text
标题/文件名包含目标实体：最高
正文包含目标实体：较高
只有通用服务器词：低
没有实体命中：直接丢弃
```

### P5：增加检索诊断与评测

在来源卡片或调试接口中展示：

```text
命中实体
命中字段
BM25 分数
向量分数
RRF 排名
Rerank 分数
是否通过实体过滤
```

建立固定评测集：

| 查询 | 期望结果 |
| --- | --- |
| aims 服务器信息 | 只返回 aims 项目相关来源 |
| 青云官网服务器信息 | 只返回青云官网相关来源 |
| 帮我查服务器 | 不应无差别返回所有项目 |
| aims 数据库账号 | 只返回 aims 的数据库信息 |

核心指标：

```text
Precision@5
Recall@5
MRR
nDCG
实体命中率
跨项目误召回率
```

## 7. 推荐实施顺序

1. 将 BM25 从纯 OR 查询改为“实体 must + 业务词 should”。
2. 审核 `aims项目`，添加 `aims` 等别名。
3. 将 `RAG_TREE_MODE` 从 `shadow` 切换到 `boost`。
4. 为空结果和低置信结果增加明确状态，禁止无差别降级。
5. 增加 content 检索的实体命中和向量分数门槛。
6. 启用 Reranker。
7. 建立固定查询评测集并纳入回归测试。

## 8. 预期结果

优化后，`aims服务器信息` 应只返回：

```text
与 aims 项目关联的服务器、部署、账号、RDS、Redis 信息
```

青云官网或其他项目的服务器内容，只有在查询中明确提到对应项目，或者同时命中目标实体时，才应进入结果。

## 9. 需要确认的问题

- `aims` 的正式 Entity 名称是 `aims项目`、`AIMS` 还是其他名称。
- 是否允许自动把高置信候选实体升级为正式实体。
- 首版使用外部 Reranker，还是先采用规则重排。
- 实体严格过滤是否对低置信查询启用，还是只对明确的专有名词启用。
