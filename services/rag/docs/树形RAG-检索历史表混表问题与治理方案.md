# 树形RAG 检索历史表（search_history）混表问题与治理方案

- **状态**: Phase 1 已落地（读侧闸门 + 实测修正）；Phase 2~6 待两分支真的合并时执行；合并已做过一次完整 dry-run 验证（见 §10）
- **日期**: 2026-10-09
- **范围**: RAG 检索观测口径、非检索操作审计、分支合并协调
- **关联文档**: 树形RAG-指标暴露与口径修正-交付文档.md、树形RAG实施计划.md、树形RAG-Phase2-窗口挂载与质量体系-交付文档.md
- **涉及分支**:
  - `codex/tree-rag-v2`（本方：树形检索、实体定位、指标口径修正）
  - `feature/agent-main`（李帅，`2385af7` "feat: expand agent person and report workflows"：联系人工具、scope 导出）

## 1. 结论摘要

`rag_mvp.search_history` 名义上是"检索记录"，实际上被三类流量共用：

1. 真实检索（做实体定位与排序）；
2. 联系人工具的**全量 scope 导出**（`scope_export`，按页写入）；
3. 联系人工具的**锚点上下文读取**（`context_scope`，当前未写 execution_path，落库为 NULL）。

后果是已经发生的事故：真实的实体定位失败率 96.24% 被导出行稀释成 16.9%，
`high_no_entity_match` 告警长期不触发。根因不是"混表"本身，而是
**判别字段可空且无约束 + 消费方默认全表**。

治理方向：

- 立即（读侧）：把指标分母从"排除黑名单"改成"检索路径白名单"，未知路径一律不计入；
- 目标（写侧）：非检索的 scope 读取写入独立的审计表，`search_history` 回归检索观测单职责；
- 过渡：不改 `search_history` 的 DDL，避免打断仍在线上运行的导出构建；
- 合并：先合 `feature/agent-main`，`tree-rag-v2` 再 rebase，在同一次冲突解决中完成写入切换。

## 2. 现象与证据

### 2.1 指标被稀释（已发生）

36 小时窗口内 `rag_mvp.search_history` 的执行路径分布：

```text
scope_export      1974 行   ← 约 90%，来自联系人工具的导出
tree_shadow        273 行
metadata_filter     81 行
```

修正前后对比（同一真实作用域）：

| 指标 | 修正前 | 修正后 |
|---|---:|---:|
| `search_query_count` | 1319 | 1278（总流量） |
| `search_retrieval_query_count` | 不存在 | 133 |
| `search_no_entity_match_rate` | 0.097 | 0.9624 |
| `search_resolved_entity_rate` | 0.0 | 0.0 |
| `alert_count` | 0 | 1（`high_no_entity_match`） |

两个数字自相矛盾（命中率 0、未命中率却"只有" 16.9%）正是稀释的直接表现。
真实情况是这些作用域的 `entity_count` 为 0，定位不可能成功。

### 2.2 代码证据

写入方（三处）：

| 写入方 | 位置 | execution_path |
|---|---|---|
| 检索 | `services/rag/app/application/rag_service.py` `search()` | `traditional` / `tree_shadow` / `tree_boost` / `metadata_filter` |
| 全量导出 | `feature/agent-main` `rag_service.py` `export_scope()` | `scope_export` |
| 锚点上下文 | `feature/agent-main` `rag_service.py` `context_scope()` | **未传，落库 NULL** |

读取方（两处，均在 RAG 内部）：

| 读取方 | 位置 | 用途 |
|---|---|---|
| `list_search_diagnostics()` | `services/rag/app/infrastructure/persistence/mvp.py` | /metrics 的树质量指标与告警，最近 24 小时、最多 2000 行 |
| `list_active_scopes()` | 同上 | /metrics 枚举"有活动的 scope"，UNION `search_history` / `entity_scan_runs` / `entity_registry` |

除这两个读取点外，全仓没有其它消费者：没有用户可见的搜索历史、没有外键引用，
QA 对话历史在 `qa_conversations` / `qa_messages`，Agent 记忆在 agent 服务自己的表，
均与 `search_history` 无关。

### 2.3 字段层面的问题

`db/migrations/20260926_rag_mvp_schema.sql` 中：

```sql
execution_path VARCHAR(32),   -- 可空、无 CHECK、无索引
filters JSONB NOT NULL DEFAULT '{}'::jsonb,
```

- `execution_path` 是唯一的"类型线索"，但它可空、没有约束、没有索引；
- `filters.entry` 埋在 JSONB 里，不可索引，也没有消费方用它做分类；
- 因此"检索行 vs 审计行"在数据层没有机器可判别的边界，全靠调用方自觉。

### 2.4 迁移误伤先例

`tree_mode` 的 CHECK 曾经只允许 `off/shadow/boost`，而 tree-rag-v2 把取值改名为
`off/shadow/tree`，导致该模式下 search_history 的插入全部因约束失败而静默丢失
（检索本身正常）。修复见 `db/migrations/20261011_tree_rag_search_history_tree_mode.up.sql`。
同一套 DDL 卡多种流量时，这类误伤会反复发生。

### 2.5 两分支冲突现状

- `feature/agent-main` 领先 main 3 个提交，已部署构建在共享 PostgreSQL 上写 `scope_export` 行；
- `codex/tree-rag-v2` 包含实体定位、tree 模式改名、指标口径修正，均未进 main；
- 两条分支相对共同祖先的实际重叠面（2026-10-09 用 `git merge-tree` 实测，**修正初版的
  "27 个重叠文件"**）：`codex/tree-rag-v2` 改 96 个文件、`feature/agent-main` 改 130 个，
  **共同改动的只有 10 个**，其中 **真正冲突 1 个**（`docker/docker-compose.yml`），
  其余 9 个 git 自动合并通过：

```text
apps/web/src/api/rag.ts                             自动合并
docker/docker-compose.yml                           冲突（两边都在迁移链尾部追加）
services/rag/app/application/rag_service.py          自动合并
services/rag/app/application/runtime.py              自动合并
services/rag/app/config.py                           自动合并
services/rag/app/domain/rag.py                       自动合并
services/rag/app/infrastructure/persistence/mvp.py   自动合并
services/rag/app/infrastructure/rag_elasticsearch.py 自动合并
services/rag/tests/test_mvp_retrieval.py             自动合并
services/rag/tests/test_mvp_runtime.py               自动合并
```

- 那处冲突是**纯追加**：两边都在 `20261007_wechat_control_flow.up.sql` 之后往
  `knowledge-migrate` 的命令链与卷挂载列表里追加迁移（我们 6 个 tree-RAG 迁移、
  他们 2 个 contact/wechat 迁移），解决方式就是把两份都按文件名时间序留下。
  迁移文件**无同名**。

## 3. 影响

1. **指标与告警失真**：分母被非检索行撑大，定位失败率被掩盖，告警失效。
2. **延迟口径失真**：导出是整 scope 扫描，耗时进入 p50/p95，既可能掩盖检索回归，也会错误放大延迟。
3. **保留期打架**：检索观测 24 小时足够，导出审计通常需要更久；同表只能取一个策略。
4. **体积与基数不对称**：导出"每人每页一行"，观测值已是检索流量的 5~6 倍；上生产后差距更大。
5. **约束与迁移互相误伤**：见 2.4，任何一方的取值/约束变化都会波及另一方的写入。
6. **合并冲突**：两条分支改同一批 RAG 核心文件，冲突不可避免。

## 4. 目标形态

```text
search_history        = 只存检索观测（做实体定位与排序的请求）
scope_access_log（新） = 只存非检索的 scope 读取（export / context）
读侧                   = 白名单：只统计检索路径，未知路径不计入并单独计数
```

写入仍由 RAG 服务完成：授权与读取都发生在这里，"全量读走某人数据"这类敏感操作的留痕
应当留在数据侧，这是防御性审计的合理位置。

## 5. 实施计划

### Phase 0：冻结契约（不做代码）

- 明确 `search_history` = 检索观测，非检索 scope 读取 = 审计流；
- 定义检索白名单：`traditional` / `tree` / `tree_shadow` / `tree_boost` / `metadata_filter`；
- 定义审计表名与取值：`rag_mvp.scope_access_log`，`access_kind ∈ {export, context}`；
- 约定合并顺序：先 `feature/agent-main`，再 `tree-rag-v2` rebase。

### Phase 1：读侧白名单止血 —— **已落地（2026-10-09，commit 58089cb）**

`services/rag/app/application/tree_metrics_service.py`：

- `NON_RETRIEVAL_PATHS` 排除法已改为 `RETRIEVAL_PATHS` 白名单；未知路径默认不计入；
- 比率与延迟只统计白名单行；`query_count` 保留总流量；
- **但白名单本身不够，实际落地的是两道闸门**：

  1. 路径在白名单内，且
  2. `locate_ms > 0`（确实跑过定位）

  第 2 条是实测逼出来的：入口隔离之后 `traditional` 同时覆盖"定位过又降级"和
  "hybrid 入口根本不定位"，而真实库里 `metadata_filter`（sources 入口）有 382 行、
  从不定位 —— 按本方案初版的路径名单会把它们算进定位率，等于换了个来源的稀释。
  `locate_ms` 是同一事实的直接证据，也因此不再依赖路径名。

- 实测（同一作用域）：`query_count = 958`（全部流量）而
  `retrieval_query_count = 13`（只有真正定位过的 13 次树检索），比率 1.0 表示这 13 次
  都没命中实体 —— 注册表为空时期的正确读数。
- 新增用例覆盖四种行：定位后降级、hybrid 不定位、`execution_path` 为空、
  另一个构建写的 `context_scope`；只有第一种计入。

**仍未做**：`unknown_path_count` 哨兵。语义要按下面重定义后再加，否则会误报。

> `unknown_path_count` 的正确语义是"**出现了白名单外的路径名**"，不是"有多少行没定位"。
> 后者会把搜索框每一次 hybrid 请求都算进去（现在每次都是不定位的），告警天天响、
> 最后被无视。真正值得报警的是"又一个构建开始往这张表写"——考虑到这张表已经两次
> 被别的构建写入（`scope_export` 1974 行、`metadata_filter` 382 行），这个哨兵值得单独做。

### Phase 2：新增独立审计表（纯加法迁移，不改 search_history DDL）

```sql
CREATE TABLE rag_mvp.scope_access_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (scope_type || ':' || scope_id::text) STORED,
    access_kind VARCHAR(16) NOT NULL,          -- export | context
    request_id VARCHAR(100),
    result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
    page_offset INTEGER,
    page_size INTEGER,
    has_more BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT scope_access_log_scope_type_chk CHECK (scope_type IN ('organization','user')),
    CONSTRAINT scope_access_log_kind_chk CHECK (access_kind IN ('export','context')),
    CONSTRAINT scope_access_log_result_count_chk CHECK (result_count >= 0),
    CONSTRAINT scope_access_log_duration_chk CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT scope_access_log_filters_object_chk CHECK (jsonb_typeof(filters) = 'object')
);

CREATE INDEX scope_access_log_scope_created_idx
    ON rag_mvp.scope_access_log (scope_type, scope_id, created_at DESC);
CREATE INDEX scope_access_log_kind_created_idx
    ON rag_mvp.scope_access_log (access_kind, created_at DESC);
CREATE INDEX scope_access_log_request_idx
    ON rag_mvp.scope_access_log (request_id);
```

- `mvp_ports.py` 增加 `record_scope_access(...)` 协议；
- Postgres 与 InMemory 两个仓储实现同步补齐；
- 这张表是新增的，`search_history` 一行 DDL 都不改，线上导出构建不受影响。

### Phase 3：写入方切换（随两分支 rebase 一起做）

- `export_scope()`：不再调用 `record_search`，改调 `record_scope_access`，
  记录 `access_kind="export"`、`page_offset`、`page_size`、`has_more`；
- `context_scope()`：改调 `record_scope_access`，`access_kind="context"`，
  记录 anchors/radius；它当前缺失 execution_path 的问题随之消失；
- `search()` 保持不变，成为唯一的 `search_history` 写入方；
- 过渡期兼容：旧构建继续写 `scope_export` 行也没关系，Phase 1 白名单会忽略，Phase 5 会清理。

### Phase 4：消费方对齐

- `list_active_scopes()` 增加对 `scope_access_log` 的 UNION；
  否则只有消息、没有实体/扫描记录的 scope 会从 /metrics 消失，而那正是定位率 0、
  最需要被观测的作用域。更彻底的做法是后续改为从 `chunks` 按 scope 派生。
- 审计查询单独提供口径（谁、何时、哪个 scope、多少条、第几页），不与检索指标混用。

### Phase 5：保留期与清理

| 表 | 建议保留期 | 清理方式 |
|---|---|---|
| `search_history`（只剩检索） | 90 天，且不得低于 7 天（覆盖 24h 指标窗口 + 排障） | 每日低峰分批 DELETE（5000 行/批）；量级上来后改为按 `created_at` 月分区、DROP 旧分区 |
| `scope_access_log` | 按合规要求，建议 180 天 | 独立清理任务或分区，与检索表互不影响 |

- 新增配置：`RAG_SEARCH_HISTORY_RETENTION_DAYS`、`RAG_SCOPE_ACCESS_RETENTION_DAYS`；
- 清理逻辑不进请求路径（运维 cron 或独立 lane）；
- 清理前先 `SELECT COUNT(*)` 预估影响，再分批执行。

### Phase 6：两分支合并与发布

1. 先合 `feature/agent-main`（+3 commits）到 main；
2. **不要把 `codex/tree-rag-v2` rebase**（修正初版建议）：32 个提交逐个重放会让
   docker-compose 那处冲突反复出现，并重写已推送、已被 review 的历史。**改用 merge**，
   一次解决。语义上也不需要 rebase——两条分支的重叠面只有 10 个文件、1 个真冲突。
3. 冲突解决：`docker/docker-compose.yml` 两处（命令链与卷挂载）按"两边都保留 +
   文件名时间序"合并。实测顺序见 §10。
4. Phase 1 已经独立落地（见上），所以这次合并**只剩 Phase 3 / Phase 4** 可选；
   建议把它们拆成单独一次改动，别塞进合并里（白名单已经兜住指标污染，不急于同批）。
5. 验证要**在合并后的树上跑**（不是各自分支上）。实测结果见 §10：RAG 240 / agent 989 /
   collector 38 / web 76，Go 两侧 build+test 全绿。
6. 部署到共享库环境：**先迁移后起服务**——`20261011_tree_rag_search_history_tree_mode`
   必须在 agent 构建写入 `search_history` 之前生效，否则会撞旧 CHECK（静默丢行的那个坑）。
   观察一个完整窗口后再考虑可选的 `search_history` 分区。

## 6. 验收标准

- 导出/上下文在 `search_history` 的新增行数为 0（历史遗留行被白名单忽略）。
- 无实体作用域的 `no_entity_match_rate` ≈ 1.0，`high_no_entity_match` 能正常触发。
- `unknown_path_count` 为 0；不为 0 即说明存在漏标写入方，告警暴露。
- /metrics 的作用域数量不因拆分减少（Phase 4 生效）。
- 线上导出构建在整个过程中无插入报错、无审计丢失。
- 导出审计可在 `scope_access_log` 查询：谁、何时、哪个 scope、多少条、第几页。

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 切换期间审计缺失 | 旧行仍在 `search_history`（被白名单忽略、不删），新行进新表，不做双写 |
| 合并冲突再次波及 | 小分支先合、大分支单次 rebase、冲突内容机械化 |
| 指标作用域枚举变少 | Phase 4 同批完成 |
| 清理误删指标窗口内数据 | 保留期下限 7 天 + 分批删除 + 删除前计数 |
| 新表未按预期写入 | 保留 Phase 1 白名单与 `unknown_path_count` 双保险 |

回滚：Phase 1 是读侧常量切换；Phase 2/3 是新增表与写入路径，回滚只需停写新表并恢复旧写入，
检索功能本身不受影响。

## 8. 备选方案（团队坚持单表时）

在 `search_history` 上增加强制判别列，而不是拆流：

- 新增 `kind`（`retrieval` / `export` / `context` / `unknown`）+ `(kind, created_at)` 索引；
- 提供 `search_history_retrieval` 视图（`WHERE kind='retrieval'`），消费方只读视图；
- **必须分两阶段**：先加可空列 + 视图 + 写入方补齐，等所有写入方上线、`kind IS NULL` 归零后，
  再加 `NOT NULL` + CHECK 并回填历史行；
- 一步到位加 `NOT NULL` 会直接打断仍在运行的导出构建，重复 `tree_mode` CHECK 的静默失败事故。

该方案可接受，但保留期与增长曲线仍然耦合在同一张表上。

## 9. 附：涉及文件与代码位置

```text
db/migrations/20260926_rag_mvp_schema.sql                    search_history DDL
db/migrations/20261011_tree_rag_search_history_tree_mode.up.sql  tree_mode 约束修复
services/rag/app/application/rag_service.py                 search() / export_scope() / context_scope()
services/rag/app/application/tree_metrics_service.py        summarize_search() / evaluate_alerts() / NON_RETRIEVAL_PATHS
services/rag/app/infrastructure/persistence/mvp.py          record_search() / list_search_diagnostics() / list_active_scopes()
services/rag/app/routers/health.py                          GET /metrics
services/agent/app/capabilities/person.py                   联系人工具主路径（调用 scope 导出）
services/agent/app/capabilities/person_scope.py             分页导出封装
services/agent/app/capabilities/form.py                     表单填充（多次分页导出的实例）
```

## 10. 合并验证（2026-10-09 实测 dry-run）

在临时 worktree（`D:\agentworkspace\_merge-check`，分支 `integration/merge-check`）里
完整跑了一遍，**没有碰 main、没有推远端**：

```text
1. worktree 从 origin/main (3525779) 建
2. merge origin/feature/agent-main   -> 干净，零冲突（1c5afc5）
3. merge codex/tree-rag-v2           -> 只冲突 docker/docker-compose.yml
4. 手工解决：迁移链与卷挂载两边都保留、按文件名时间序（4a9c5d1）
5. 在合并后的树上跑测试
```

在合并后的树上跑的验证：

| 套件 | 结果 |
|---|---|
| RAG（pytest） | **240 passed, 4 skipped**（我方 235 + agent 侧新增的 5） |
| Agent（pytest） | **989 passed, 92 skipped** |
| Web（vue-tsc + npm test） | 类型检查无输出；**76 passed** |
| Knowledge（Go） | build ✅，test 全绿 |
| Core（Go） | build ✅，test 全绿 |
| WeChat collector（pytest） | **38 passed** |

两个环境注意项（都不是失败）：

- agent 侧新增依赖 `python-docx>=1.2.0`，本地 agent venv 里没有 → 合并后需要
  `uv sync --project services/agent`。本次是在 worktree 里建独立 venv，没动主检出的；
- collector 的依赖组里没有 pytest，跑它的测试要用 `uv run --with pytest`。

**语义复核**（自动合并通过 ≠ 正确）：agent-main 基于 main，**仍在用 Phase 0 删掉的
整套旧接口**（`branch_keys` / `BranchMatch` / `tree_nodes` / `_resolve_branches` /
`tree_branch_weight` / `tree_max_branches` / `match_chunk_branches`）。在合并结果树里
逐个 grep：**零残留**（`tree_nodes` 只出现在一句"它已被删除"的注释里）；他们的
`export_scope` / `context_scope` 存活，且调用的是我们改名后的内部函数；通道选择函数是
`select_retrieval_channels`，没有旧的 `select_tree_channels`。

冲突解决后的迁移链顺序（26 条，末尾 8 条即本次合并新增的）：

```text
20261007_wechat_control_flow.up.sql
20261007_contact_name_core.up.sql            ← agent-main
20261009_tree_rag_v2_schema.up.sql           ← tree-rag-v2
20261009_wechat_contact_book.up.sql          ← agent-main
20261010_tree_rag_scan_watermarks.up.sql
20261011_tree_rag_eval.up.sql
20261011_tree_rag_search_history_tree_mode.up.sql
20261012_tree_rag_scan_runs.up.sql
20261013_tree_rag_review_duration.up.sql
```

**结论：这次合并已验证可行**——只有 1 处冲突、解法机械（两边保留 + 时间序），
合并后的六套测试全绿，且不存在"引用了已删除接口"的暗雷。

**版本控制**:
- v1.0 (2026-10-09): 初始版本，问题复盘与治理方案
- v1.1 (2026-10-09): Phase 1 标记为已落地并修正为"白名单 + locate_ms 双闸门"；
  用 merge-tree 实测修正冲突面（27 → 10 个重叠、1 个真冲突）；
  合并策略由 rebase 改为 merge；新增 §10 合并 dry-run 验证结果
