# 树形RAG 指标暴露与检索口径修正 交付文档

- **阶段**: Phase 2 质量体系的收尾（实施计划 4.2.3「指标看板」的前置条件）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: `/metrics` 端点完成并实测；Grafana 看板未做（见第 6 节）
- **关联文档**: 树形RAG实施计划.md (v2.4)、树形RAG-Phase2-窗口挂载与质量体系-交付文档.md

## 1. 起点：一条死配置

计划 4.2.3 要求「指标接入 + Grafana 看板」。核对现状时发现两件事：

1. 仓库里既没有 Prometheus 也没有 Grafana，没有任何监控栈配置。
2. `RAG_METRICS_ENABLED` 声明在 `app/config.py`，但**全仓没有任何地方读它**，
   也没有任何端点把 `TreeMetricsService` 的数字暴露出去。

也就是说指标只在进程内算，出不了服务边界。先补暴露端点，再谈看板。

## 2. 交付物

### 2.1 `GET /metrics`（Prometheus 文本协议）

`app/routers/health.py`

```text
GET /metrics
Header: X-RAG-Internal-Token: <RAG_INTERNAL_TOKEN>
Content-Type: text/plain; version=0.0.4; charset=utf-8
```

三种失败语义分开，避免把配置问题误读成空数据：

| 情况 | 返回 |
|---|---|
| 未带 / 带错 token | 403 `internal token required` |
| `RAG_INTERNAL_TOKEN` 未配置 | 500 `RAG_INTERNAL_TOKEN is not configured` |
| `RAG_METRICS_ENABLED=false` | 404 `metrics disabled` |

用内部 token 而不是开放端点，是因为序列里带 `scope` 标签，而 scope id 是租户标识。

### 2.2 活跃 scope 枚举

`BranchRepository.list_active_scopes(limit)`（`mvp.py` 两个实现 + `mvp_ports.py` 协议）

抓取请求本身没有租户上下文，所以 scope 从三张记录过工作的表里取，按最近活动排序：

```sql
search_history   UNION ALL  entity_scan_runs  UNION ALL  entity_registry
  -> GROUP BY scope_type, scope_id
  -> ORDER BY MAX(seen) DESC NULLS LAST
  -> LIMIT %s
```

默认 `DEFAULT_SCOPE_LIMIT = 20`：一次抓取会为每个 scope 跑一遍 snapshot，而 snapshot
是若干条查询，扇出必须封顶。单个 scope 的完整明细仍可从带鉴权的管理端点拿。

### 2.3 渲染

`app/application/tree_metrics_service.py`

```text
prometheus(scope_limit)   逐 scope 取 snapshot，交给 render_prometheus
flatten_metrics(snapshot) 把嵌套 snapshot 压成一组扁平数值
render_prometheus(samples) 输出 # HELP / # TYPE / 逐 scope 的样本行
```

全部是 gauge：计数、比率、延迟分位数都是时点值。当前 20 个指标族：

```text
rag_tree_entity_count / message_count / mounted_chunk_count / mount_count
rag_tree_mount_coverage / pending_candidate_count / relation_count
rag_tree_entities_missing_embedding / alert_count
rag_tree_search_query_count / search_retrieval_query_count
rag_tree_search_no_entity_match_rate / search_resolved_entity_rate
rag_tree_search_l4_invocation_rate
rag_tree_search_latency_p50_ms / search_latency_p95_ms
rag_tree_scan_window_count / scan_empty_window_ratio
rag_tree_scan_mount_count / scan_candidate_count
```

没有活动的 scope 也会补 0 值，避免看板上出现"租户没数据"和"租户挂了"分不清的缺口。

## 3. 口径修正：`scope_export` 污染了检索率

端点一通就暴露了一个数字问题：真实库里 `resolved_entity_rate` 恒为 0，而
`no_entity_match_rate` 只有 16.9%，两者自相矛盾。

追下去发现检索窗口里的行按执行路径分布是这样的（36 小时，`rag_mvp.search_history`）：

```text
scope_export      1974 行
tree_shadow        273 行
metadata_filter     81 行
```

`scope_export` 是**批量导出**，不是检索：它从不做实体定位，`resolved_entity_count`
天然为 0。它占了 90% 的样本，于是把每个比率都稀释了。更值得注意的是，
`scope_export` 这个字符串在当前代码里根本不存在，git 历史里也没有过——这批行来自
另一个构建往同一个 PostgreSQL 写的数据。

修正做法（`summarize_search`）：

```text
NON_RETRIEVAL_PATHS = {"scope_export"}

query_count             = 全部行（保留总流量口径）
retrieval_query_count   = 排除非检索路径后的行数
no_entity_match_rate    = no_entity_match / retrieval_query_count
resolved_entity_rate    = 命中实体的行 / retrieval_query_count
l4_invocation_rate      = 调用 L4 的行 / retrieval_query_count
latency_ms p50/p95/max  = 只统计检索行
```

`evaluate_alerts` 的 `high_no_entity_match` 也改为以 `retrieval_query_count` 为闸门：
一个只有导出的窗口没有定位工作可判断，它的 0% 不能被读成"健康"。

修正前后，同一个真实作用域的对比：

| 指标 | 修正前 | 修正后 |
|---|---|---|
| `search_query_count` | 1319 | 1278（总流量） |
| `search_retrieval_query_count` | 不存在 | 133 |
| `search_no_entity_match_rate` | 0.097 | **0.9624** |
| `search_resolved_entity_rate` | 0.0 | 0.0 |
| `search_latency_p50_ms` | 2253 | 2713 |
| `search_latency_p95_ms` | 7661 | 5266 |
| `alert_count` | 0 | **1**（`high_no_entity_match`） |

修正后的数字说的是实话：这些作用域的 `entity_count` 是 0，注册表里没有实体，
定位当然一个也命中不了。以前这个状态被导出流量掩盖成 16.9% 的"还行"。

**注意**：Phase 2/Phase 3 交付文档里引用的"475 次查询、降级率 16.84%"是按旧的
全入口口径算的，仍然可作历史记录，但**不能**当作树的定位质量来读。

## 4. 附带修正：视觉模型配置

`services/rag/.env` 的 `RAG_VISION_*` 原先指向
`https://best-api.huggingclaw.store/v1` + `deepseek-flash`。实测该模型自身不具备
视觉能力（无图对照组会凭空编造内容），网关侧疑似做了图像转文本预处理，且真实照片
会打满输出上限被截断（`finish_reason=length`，600 completion 里 507 是 reasoning）。
`VisionClient` 不发 `thinking: disabled`，截断的正文会被当成 `VisionError` 静默丢弃。

已改为 `qwen-vl-max` + `https://dashscope.aliyuncs.com/compatible-mode/v1`
（DashScope key 与 embedding/rerank 复用同一把）。用服务自己的 `VisionClient`
跑真实照片：3.7s 返回完整中文描述，`image_tokens` 正常计入。

`.env` 不被 git 跟踪，此改动只落在本地运行环境，随本文件记录。

## 5. 验收证据

```text
pytest -q    ->  152 passed, 4 skipped
```

新增用例覆盖：导出不稀释比率与延迟、纯导出窗口不误报警、扁平化包含新指标、
以及 `flatten_metrics` 对空 snapshot 的补 0。

真实端点（RAG 本地 8000，内部 token 鉴权）：

```text
GET /metrics 无 token           -> 403 {"detail":"internal token required"}
GET /metrics 带内部 token       -> 200 text/plain; version=0.0.4
指标族数量                      -> 20
活跃 scope                      -> 15
```

抓取耗时约 9~10s（15 个 scope × 每 scope 若干查询，含每次全表分位计算），
这也是 `DEFAULT_SCOPE_LIMIT=20` 的原因：抓取间隔不要低于 1 分钟。

## 6. 未做与后续

| 项 | 说明 |
|---|---|
| Grafana 看板 | 仓库没有监控栈。引入 Prometheus + Grafana 属于新增部署组件，需要先确认再动；`/metrics` 已是其前置条件 |
| 抓取性能 | 15 scope 约 9~10s。若 scope 数继续增长，应把 snapshot 改成单条聚合 SQL，而不是逐 scope 多查询 |
| 历史数据口径 | 旧行的 `scope_export` 仍在库里，只影响 `query_count` 总流量，不再影响比率 |
| 定位质量验收 | 需要真实查询集。修正后的分位数才是"常规路径 p95 ≤ 80ms"该用的口径 |

## 7. 变更文件

```text
services/rag/app/application/tree_metrics_service.py    Prometheus 渲染 + 检索口径修正
services/rag/app/application/mvp_ports.py               list_active_scopes 协议
services/rag/app/infrastructure/persistence/mvp.py      两个仓储的 list_active_scopes
services/rag/app/routers/health.py                      GET /metrics 端点
services/rag/tests/test_tree_metrics_service.py         +8 例
services/rag/.env                                       视觉模型改 qwen-vl-max（未跟踪）
```
