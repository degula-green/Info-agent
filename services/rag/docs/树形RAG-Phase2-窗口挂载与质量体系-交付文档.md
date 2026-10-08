# 树形RAG Phase 2 交付文档：窗口挂载与质量体系

- **阶段**: Phase 2 / 窗口挂载与质量体系
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-10
- **状态**: 窗口挂载与无标注指标完成；标注集与看板待建（见第 4 节）
- **关联文档**: 树形RAG实施计划.md (v2.4)、实体节点生成方案-MVP.md (v2.2)、树形RAG-Phase1-定位管线与检索接入-交付文档.md

## 1. 阶段目标

把隐式主题消息挂载到实体节点，建立置信度分级与可度量的质量体系。

## 2. 交付物

### 2.1 扫描水位线（迁移）

| 文件 | 说明 |
|---|---|
| `db/migrations/20261010_tree_rag_scan_watermarks.up.sql` | 新建 `entity_scan_watermarks` |
| `db/migrations/20261010_tree_rag_scan_watermarks.down.sql` | 回滚 |

已接入 `docker-compose.yml` 与 `docker-compose.server.yml` 两处迁移清单。

**为什么是水位线而不是布尔标记**：布尔位只能说"看过"，失败时要么丢消息
（标了却没处理成功），要么永远重复（没标）。水位线只在窗口成功落库后推进，
天然可重试、可续跑。`set_scan_watermark` 用 `GREATEST` 推进，保证水位不会
因乱序批次倒退。

### 2.2 窗口扫描（`app/application/window_scan_service.py`）

```text
EntityWindowScanWorker.run_once(conversation_limit)
  1. list_scan_conversations    找出有消息晚于水位线的会话
  2. list_conversation_chunks   取水位线之后的消息（按 sent_at 排序）
  3. build_windows              20 条窗口 / 10 条步长，且必定覆盖尾部
  4. 每个窗口：
       build_extraction_prompt   带上 scope 内 known 实体摘要（含 ID、别名）
       extractor.extract         调 LLM 提取实体与关系
       clean_entities            类型白名单 + 凭据过滤 + 去重
       clean_relations           关系枚举校验 + 自环/空端过滤
       find_entities_by_normalized  名称解析（含关系端点）
       显式挂载（文本匹配）       confidence 0.95~1.0，method=explicit
       窗口挂载（已知实体）       method=window_batch，strong 0.85 / weak 0.65
       未知实体                 写入 entity_candidates 等审核
       关系                      两端都能解析才写 entity_relations
  5. set_scan_watermark         推进到本批最后一条消息
```

### 2.3 置信度分级

| 档位 | 置信度 | 来源 |
|---|---|---|
| `explicit` | 0.95 ~ 1.0 | 文本匹配（精确 1.0 / 别名 0.95） |
| `window_strong` | 0.85 | 模型置信度 ≥ 0.8 的窗口主题 |
| `window_weak` | 0.65 | 模型置信度 < 0.8 的窗口主题 |
| `llm_infer` | 0.50 | 模型推断（当前未产生，预留给关系推断） |

### 2.4 挂载写入语义

| 方法 | 语义 | 用于 |
|---|---|---|
| `replace_chunk_mounts` | 让 chunk 的挂载集合**等于**入参 | 索引路径（chunk 文本决定归属） |
| `merge_chunk_mounts` | 只增不删 | 窗口扫描（窗口只看到会话的一部分，替换会抹掉别的窗口的贡献） |

两者共用同一套 upsert 规则：`confidence` 取 `GREATEST`，`mount_method` 按
`explicit > window_batch > llm_infer` 保留更强来源。这是 50% 重叠窗口幂等的
基础。

### 2.5 关系写入

`upsert_entity_relation` 在唯一约束上做 `ON CONFLICT`，置信度取 `GREATEST`，
`evidence_chunk_ids` 取并集——同一条关系会在多个重叠窗口被重复观察到，普通
upsert 会让后一次低置信度覆盖高置信度，并丢掉之前的证据。

### 2.6 无标注指标（`app/application/tree_metrics_service.py`）

这些指标不需要标注数据，因此第一天就能看：

```text
mount_coverage                有挂载的消息占比（冷启动核心信号）
mount_methods                 按挂载方式的计数与置信度分布
entities_by_domain            各类型实体数
relations_by_type             各关系类型边数
entities_missing_embedding    没有向量的实体数（L3 看不到它们）
pending_candidate_count       待审候选积压
scanned_conversation_count    已建立水位线的会话数
alerts                        使其它指标失去解释力的状态
```

告警判定：`no_mounts`（有实体但没有任何挂载）、`candidate_backlog`（>100）、
`entities_missing_embedding`（缺失比例 >20%）、`no_relations`。

新增接口：`GET /api/v1/admin/tree-metrics`（与其它管理接口同一套 Core 能力校验）。

### 2.8 检索侧降级率（"降级率可查"）

同一接口返回 `search` 段，来自 `search_history` 最近 24 小时的诊断：

```text
query_count              查询数
execution_paths          各执行路径占比（tree / tree_shadow / traditional / metadata_filter / scope_export）
fallback_reasons         降级原因计数（no_entity_match / empty_scope / branch_failed）
degraded_reasons         技术降级计数（embedding_failed / relation_lookup_failed 等）
resolved_entity_rate     成功定位到实体的查询占比
no_entity_match_rate     未命中实体而回落全库的比例
l4_invocation_rate       L4 被触发的查询占比
latency_ms               p50 / p95 / max
```

配套改动：检索诊断新增顶层 `locate_llm_invoked`，让 L4 调用率变成可查询的单个
标志，而不必解析嵌套的逐 mention 轨迹。

**这一条修掉了一个此前被静默吞掉的真问题**：`search_history.tree_mode` 的 CHECK
还是 `off/shadow/boost`，而 Phase 1 已把模式改名为 `tree`。结果是**所有 tree 模式
的检索记录都写入失败**，而 `record_search` 被 `except Exception: pass` 吞掉，所以
线上一直没有任何报错——观测数据静默缺失了整整一个版本。修复：迁移放宽约束
（保留 `boost` 以兼容历史行），并把静默 pass 改成 `logger.warning`。

### 2.7 并发与调度

**并发**：同一个会话的多个窗口彼此独立，用 `ThreadPoolExecutor` 并发执行，
上限取 `RAG_EXTRACT_CONCURRENCY`（默认 8）。计数不再写共享对象，改为每个窗口
返回 `WindowResult` 由调用方汇总，因此不需要锁。任一窗口失败即中止该会话批次，
水位线不推进——部分成功不能算成功。

**调度**：`MVPWorkerRuntime` 的 callback 循环按 `RAG_WINDOW_SCAN_INTERVAL_SECONDS`
（默认 300s）触发扫描，并且：

```text
- 放在后台线程执行：一次扫描要处理多个会话、可能持续数分钟，同步执行会把
  callback 通道堵住，延迟 knowledge 回写
- 同一时刻只允许一个扫描在飞行，跨过间隔也不会叠加第二个会话批次
- RAG_WINDOW_SCAN_ENABLED=false 可整体关闭
```

配置项：`RAG_WINDOW_SCAN_ENABLED` / `RAG_WINDOW_SCAN_INTERVAL_SECONDS` /
`RAG_WINDOW_SCAN_CONVERSATION_LIMIT`（默认 5）。

## 3. 验收证据

### 3.1 单元测试

```text
pytest: 114 passed, 4 skipped

新增 test_window_scan_service.py（11 例）：窗口重叠与尾部覆盖、短会话单窗口、
  类型白名单与凭据过滤、关系枚举校验、已知实体产生 window_batch 挂载、
  未知实体进候选、模型失败不推进水位线、成功后不重复扫描、
  关系端点解析、置信度只升不降、模型无输出时仍写显式挂载、prompt 带已知实体
新增 test_tree_metrics_service.py（5 例）：空 scope 不误报告警、覆盖率、
  no_mounts / candidate_backlog / entities_missing_embedding / no_relations
```

### 3.2 真实数据端到端

**第一次扫描**（尚无任何实体，模拟冷启动）：

```text
耗时 23.5s | conversations=1 windows=3 mounts=0 candidates=10 relations=0
候选（名称, 类型, 评分）:
  Castorice | person | 0.9000
  稻成      | person | 0.9000
  洗衣服    | project | 0.7000
  假期安全问题会议 | project | 0.6000
  教务系统  | project | 0.5000
```

**引入一个正式实体后重扫**（清水位线，同一批消息）：

```text
outcome = conversations=1 windows=3 mounts=60 candidates=5 relations=0
挂载分布: window_batch | 40 条 | 置信度 0.85
稻成 已不在候选区
```

40 条对应窗口内去重后的消息数（20+20+15 三个窗口、50% 重叠、去重后 40 条），
**证明重叠窗口产生了幂等挂载而不是重复行**。

两次验证后测试数据均已清理，`tree-metrics` 回到全零且不误报告警。

### 3.3 定时扫描在真实环境中的运行证据

重启 rag-worker 后（首次 tick 立即触发），无需人工调用即产生数据：

```text
entity_scan_watermarks     = 3 个会话
entity_candidates          = 48 条（去重后）
entity_candidate_mentions  = 141 条
chunk_branches             = 0（注册表为空，冷启动全部落候选，符合预期）
tree-metrics(org scope)    = 2 个会话已扫、5 条待审候选、覆盖率为 0 且无误报告警
```

### 3.4 开发过程中发现并修正的四个问题

```text
1. 候选评分恒为 0.5
   建表时的默认值被写死在 INSERT 里，审核页的"置信度"因此对所有候选都是 50%。
   改为写入模型置信度，并在冲突时取 GREATEST。
2. 关系端点解析遗漏
   lookup 只取 entities 列表里的名字；模型给出"张三 -> A项目"但没在 entities
   里重复两者时，边就被静默丢弃。改为把关系两端也算进解析范围。
3. 窗口挂载与显式挂载互相覆盖
   窗口扫描最初复用 replace 语义，会让后一个窗口抹掉前一个窗口的挂载。
   拆出 merge_chunk_mounts（只增不删）后修复。
4. 扫描阻塞回调通道
   扫描最初同步跑在 callback 循环里，一次批次持续数分钟，期间 knowledge 回写
   无法 flush。改为后台线程 + 单批次互斥后修复。
5. 检索历史写入被约束拒绝且静默吞掉
   `search_history.tree_mode` 的 CHECK 未随模式改名更新，tree 模式的记录全部
   写入失败；`record_search` 的 `except Exception: pass` 让它无声无息。已加迁移
   放宽约束，并把静默 pass 改为 warning 日志。
```

### 3.5 检索侧指标的真实数据

```
tree-metrics?scope_type=organization 的 search 段（近 24 小时）:
  query_count        475
  execution_paths    scope_export 393 / tree_shadow 80 / metadata_filter 2
  fallback_reasons   no_entity_match 80
  degraded_reasons   embedding_failed 4
  resolved_entity_rate   0.0（注册表为空，冷启动预期）
  no_entity_match_rate   0.1684
  latency_ms         p50 633 / p95 1803 / max 11138
```

**关于延迟口径的说明**：这里的 p95 覆盖全部入口（含 `scope_export` 这类批量导出），
不等于实施计划 3.4 的"常规路径 p95 ≤ 80ms"。后者需要用 `entry=global/ai` 且 L4
未触发的查询集单独测量，属于尚未完成的实测项。

## 4. 未完成项

### 4.1 标注集（Phase 2 计划内，未开始）

实施计划要求三套标注集（实体定位 / 检索效果 / 窗口挂载）与离线评估脚本。
本阶段只完成了不需要标注的第一层指标；标注集需要人工投入，尚未启动。

### 4.2 监控看板

指标接口已就绪，Grafana 面板尚未配置。

### 4.3 上量前必须补测

```text
[已完成] 用真实 20 条消息窗口复测延迟：单次约 6.5s，p95 约 7.2s
[已完成] 并发与 TPM：并发 16 无排队，TPM 约 17 万，远高于 600 会话/小时的需求
[已完成] 关闭推理对召回的影响：关闭推理后仍能稳定抽到实体（见性能实测文档）
[已缓解] 单窗口抽取不稳定：定位到主因是 prompt 里的"已知实体"列表——它没有消费者
         却显著降低稳定性。移除后空转率从 43.75% 降到 18.75%，不稳定窗口从 3/8
         降到 1/8，实体产出翻倍。残留约 1/8 的窗口仍在摇摆，建议先把空转率与
         挂载覆盖率接入监控再判断影响。详见《树形RAG-窗口扫描性能实测-交付文档.md》
```

### 4.4 已知限制

```text
- 单会话扫描上限 500 条消息，超出部分留待下一轮
- 窗口内所有消息都会挂到窗口主题实体（这是设计取舍），噪音由置信度分级控制
- 扫描线程不随 stop_event 中断：进程退出时正在进行的批次会被直接终止（水位线
  未推进，下次重扫，无数据损失）
```

## 5. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-10-10 | Phase 2 窗口挂载与无标注指标完成 |
