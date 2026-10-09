# 树形RAG系统 - 分阶段实施计划

## 文档状态

- **版本**: v2.5
- **状态**: 执行中（Phase 0-3 已交付，Phase 4 待数据）
- **更新日期**: 2026-10-09
- **目标**: 在现有 RAG 实现基础上改造出树 + 图混合检索能力，分阶段交付
- **配套文档**: 树形RAG设计方案.md、实体定位五层管线接口草案.md、interfaces/07-rag-admin-entity-tree-api.md
- **v1 存档**: 树形RAG实施计划-v1.md

## 0. v2 相对 v1 的关键变更

v1 是按“从零新建一套表”写的，与仓库现有实现冲突。v2 按已确认的决策重写：

1. **改为原地改造现有表**，不兼容旧逻辑；不新建 `entities` / `chunk_mounts` 重名表。
2. **domain 本期固定 5 类**：organization / person / project / policy / contract。
3. **实体向量存 PostgreSQL pgvector**（远端已有扩展），异步回填，不再走应用层余弦，也不放 ES。
4. **补入五层实体定位管线 L0-L5**，以 mention 为粒度，详见《实体定位五层管线接口草案》。
5. **检索过滤契约从 `branch_keys` 改为 ES 的 `entity_ids` + `entity_mounts`**。
6. **`tree_mode` 语义调整为 `off / shadow / tree`**（tree-first，无实体时降级全库）。
7. **阶段重排**：地基改造 → 定位管线 → 窗口挂载 → 关系网络 → 自动化。
8. **指标体系重构**：延迟按常规路径与 L4 升级路径拆开，新增挂载质量、降级率与跨 scope 安全指标。

## 1. 总体规划

### 1.1 实施原则

```yaml
原则1: 改造优先于新建
  - 复用 entity_registry / entity_aliases / entity_candidates / entity_review_requests
  - 复用 chunk_branches / branch_refresh_jobs / EntityMatcher / RRF 融合
  - 只在现有表上做字段级改造，避免两套实体模型并存

原则2: 地基先破后立
  - Phase 0 先完成破坏性迁移（收窄 domain、删 tree_nodes、删 branch_key）
  - 保留 chunks 与 ES 中的正文和向量，重建成本只落在树侧数据

原则3: 检索始终可用
  - 任何阶段都保留传统全库检索作为降级路径
  - 树侧异常不得导致检索失败

原则4: 指标先于优化
  - 先建立标注查询集与分层指标，再谈调参和优化
```

### 1.2 现状基线

改造前必须承认：树形 RAG 的相当一部分已经落地，本计划是**改造 + 补齐**，不是从零开发。

| 类别 | 内容 |
|---|---|
| **已落地，直接复用** | `entity_registry` / `entity_aliases`；`entity_candidates` / `entity_candidate_mentions`；`entity_review_requests` 幂等审核；`chunk_branches` / `branch_refresh_jobs`；`EntityMatcher` / `match_chunk_branches`；`RAGRetrievalService` 的 tree_mode 与 RRF 融合；管理端 entity-tree / entity-candidates 接口 |
| **需改造** | `entity_registry` 加向量与统计字段、domain 收窄 5 类；`chunk_branches` 加 `confidence` / `mount_method`、删 `branch_key` 与 tree_nodes 外键；ES chunk 文档加 `entity_ids` / `entity_mounts`；检索从 branch_keys 过滤改为实体挂载过滤；tree_mode 语义调整；候选发现从正则改为 LLM 窗口提取 |
| **需新建** | `entity_relations` 表；五层定位管线 `EntityLocator`；pgvector 实体向量回填任务；关系扩展检索 |
| **需删除** | `tree_nodes` 表及其外键 |
| **前端现状** | `apps/web/src/views/info/InfoKnowledgeStructurePage.vue` 已挂路由 `/organization/knowledge`，含「节点树 / 候选审核 / 检索测试」三个 Tab，但全部是硬编码假数据，动作只弹 toast；候选动作（试用节点 / 确认正式）与接口 07 不一致，节点树依赖将被删除的 `tree_nodes` |
| **需新建（前端）** | 最小审核页、检索诊断面板（详见 1.5） |

### 1.3 阶段划分与时间线

```text
Phase 0            Phase 1              Phase 2            Phase 3          Phase 4
地基改造           定位管线与检索接入     窗口挂载与质量体系   关系网络         自动化升级
2 周               3 周                 3 周               2 周             持续
────────>          ────────>            ────────>          ────────>        ────────>
表改造              L0-L5 定位管线        窗口 LLM 提取       关系提取         自动合并建议
数据清空            pgvector 向量         置信度分级          图扩展检索       智能消歧
ES mapping          检索接入 + 灰度       审核页 + 看板       关系质量         自动批准
```

时间估算基于现有实现可复用。若存量 chunk 规模超出预期，Phase 0 的回填和重建需要单独加缓冲。

### 1.4 里程碑定义

| 里程碑 | 时间 | 交付物 | 验收标准 |
|---|---|---|---|
| **M0: 地基就绪** | Week 2 | 表改造、数据重建、ES mapping | 迁移可正向/回滚执行，检索能力不回归 |
| **M1: 定位可用** | Week 5 | L0-L5 管线、pgvector、灰度开关 | 常规路径 p95 ≤ 80ms，降级率可控 |
| **M2: 质量可测** | Week 8 | 窗口挂载、置信度分级、审核页、指标看板 | 挂载准确率达标，审核闭环可用，指标可视 |
| **M3: 图检索可用** | Week 10 | 关系提取与图扩展检索 | 关系类查询可召回，关系准确率达标 |
| **M4: 自动化** | Week 12+ | 自动合并 / 消歧 / 批准 | 自动化准确率达标，人工量下降 |

### 1.5 前端与评估工具优先级

"要不要做可视化"实际包含四件不同的事，优先级各不相同：

| 优先级 | 事项 | 回答的问题 | 时间 | 说明 |
|---|---|---|---|---|
| 1 | 标注测试集 + 离线评估脚本 | 设计是否成立 | 现在启动，贯穿 Phase 1-2 | 不依赖 UI，是后续所有调参的依据 |
| 2 | 最小审核页 | 候选如何变成正式实体 | Phase 1 后期启动，Phase 2 并行完成 | **闭环必需**，见下方说明 |
| 3 | 检索诊断面板 | 为什么没召回到 | Phase 2 | 研发调参工具，展示 L0-L5 分层与命中对比 |
| 4 | 树可视化 | 结构长什么样 | Phase 3+，本期不做 | 产品里用户对树应"无感"，且结构仍在变化，不阻塞任何验收 |

审核页优先于诊断面板的两个依据：

```text
1. 冷启动是审核量的峰值，不是低谷。窗口扫描一旦打开，所有实体都不在 registry 里，
   每一个都是候选。用"当前 registry 为 0"判断审核量小是错的——
   那是扫描还没开，不是需求小。
2. 审核页本身是被验证对象。设计里的"审核时间 < 30 秒/实体""人工批准率 > 80%"
   只有通过 UI 才测得出来，脚本批量批准拿不到这两个数。
3. 没有审核通道，树里永远是空的，Phase 2/3 的下游全部无法验证。
```

分工：**审核页是产品闭环，面向业务人员；诊断面板是研发工具，面向研发。** 两者都要，但审核页更基础。

现有 mock 的处理（`InfoKnowledgeStructurePage.vue` 当前是活路由但全假数据，容易被误当成已完成功能）：

```text
建议：先摘掉导航入口或标注"未接入"，然后拆成两个新页面，不要在旧页面上增量改造
  InfoEntityReviewPage.vue           审核页（Phase 1 后期）
  InfoRetrievalDiagnosticsPage.vue   诊断面板（Phase 2）
  树可视化页面                        Phase 3+ 再评估

可复用:
  - 整体布局、Tab 骨架
  - 候选详情抽屉的字段组织（置信度 / 提及次数 / 证据 chunk / LLM 理由 / 建议别名 / 晋级后影响）
  - 诊断对照表结构（检索方式 / 命中排名 / 送入上下文 / 耗时）
  - tdesign 组件用法

必须重写:
  - 数据源：mock → 真实 API
  - 候选动作语义：改为接口 07 的 promote / merge / ignore / defer，去掉不存在的"试用节点"
  - 节点树数据模型：tree_nodes 已删除，改为从 entity_registry 派生
  - 诊断展示：branch key → entity_ids / entity_mounts
```

### 1.6 交付状态索引（滚动更新）

下方各阶段的任务清单是**原始排期记录，未逐条勾选**（勾选状态不反映实际进度）。
实际进度以本表为准，每一项都有 `rag/docs` 下的交付文档与验收证据。

| 阶段 | 周次 | 状态 | 交付文档 | 证据 / 阻塞 |
|---|---|---|---|---|
| Phase 0 地基改造 | Week 1-2 | ✅ 交付 | 树形RAG-Phase0-地基改造 | 迁移正向+回滚演练、脏数据清理、ES entity_ids/entity_mounts、实库应用 |
| Phase 1 定位管线 | Week 3 | ✅ 交付 | 树形RAG-Phase1 | L0-L5 管线、L4 门控与验证器、定位单测 |
| Phase 1 | Week 4 | ✅ 交付 | 树形RAG-Phase1 | pgvector 回填、ES 过滤、tree_mode 三态、residual_query |
| Phase 1 | Week 5 | ⚠️ 部分 | 树形RAG-Phase1、定位分层耗时、灰度白名单、回滚演练 | 任务清单全部完成（灰度白名单 + 采样 + 两步回滚演练均通过）；**唯一未达标项**是"常规路径 p95 ≤ 80ms"，实测 1330ms，根因是中间件在远端 |
| Phase 1（前端） | Week 5 | ✅ 交付 | 树形RAG-Phase1 §2.6 | 审核页列表 / 详情 / 四动作接真实接口 |
| Phase 2 窗口挂载 | Week 6 | ✅ 交付 | 树形RAG-Phase2、窗口扫描性能实测 | 窗口扫描、水位线、8 类关系枚举校验、挂载写入、并发定档 |
| Phase 2 | Week 7 | ✅ 交付 | 树形RAG-Phase2 | 置信度分级、候选链路、审核闭环（候选 → promote → 检索命中） |
| Phase 2 | Week 8 | ⚠️ 部分 | 树形RAG-Phase2、标注集与离线评估、指标暴露与口径修正 | 无标注指标、`/metrics`、离线评估框架就绪；Grafana 面板未配置，首批标注集待人工 |
| Phase 3 关系网络 | Week 9-10 | ⚠️ 部分 | 树形RAG-Phase3 | 关系查询 / 一跳扩展 / 方向与阈值完成；关系准确率标注待人工，低置信降权与多跳未做 |
| Phase 4 自动化 | Week 11+ | ⛔ 未开始 | — | 阻塞：`entity_review_requests = 0`、`entity_registry = 0`，监督信号与实体量都不存在；决策点 4 未满足 |

当前需要人拍板的两件事：

```text
1. 监控栈：/metrics 已就绪，Prometheus + Grafana 属于新增部署组件，尚未引入。
2. 80ms 口径：定位 p95 实测 1330ms（L0 205 + L1 137 + L2 212 + L3 470）。
   单次远端 PG 往返即 100~300ms，常规路径至少两次往返。要么把中间件拉近，
   要么把该指标改成"进程内计算耗时"口径。两套数已分别计量。
```

横向治理（不属于某个 Phase 的验收项，但决定配置与文档是否可信）：

```text
✅ 配置面治理      删除被取代的残留设置、清理 compose 失效注入，
                   并加守卫测试禁止"声明了但没人读"的设置无声增长
                   （见 树形RAG-配置面治理-交付文档.md）
✅ 指标抓取与告警  抓取配置 + 7 条告警规则，规则引用的序列与实际产出交叉校验
                   （见 树形RAG-监控抓取与告警规则-交付文档.md）
⚠️ 集成测试环境    共享开发库上有另一个部署的 worker 在消费 processing_jobs，
                   四个外部集成用例无法独占 job 生命周期；已把失败信息改成可诊断，
                   隔离库/schema 待建（见 树形RAG-集成测试环境发现-交付文档.md）
```

## 2. Phase 0: 地基改造（Week 1-2）

### 2.1 目标

```text
把现有表结构改造成新设计要求的形态，清空树侧派生数据并重建，
让 Phase 1 可以直接在正确的地基上开发，不背旧逻辑。
```

本阶段是整个项目风险最高的部分：包含破坏性迁移、数据清空和 ES mapping 变更，必须独立完成并验证，不与功能开发混在一起。

### 2.2 交付物

#### 2.2.1 表改造清单

迁移文件按仓库现有约定放在**仓库根目录** `db/migrations/`，采用日期命名：

```text
db/migrations/20261008_tree_rag_v2_schema.up.sql
db/migrations/20261008_tree_rag_v2_schema.down.sql
```

**0) 前置：启用 pgvector（必须最先执行）**

2026-10-09 连接目标库（`info-agent`，PostgreSQL 16.14）实测：

```yaml
pg_available_extensions: vector 0.8.5 已随服务端安装
pg_extension:           尚未启用（当前仅 pg_trgm 1.6 / pgcrypto 1.3 / plpgsql 1.0）
迁移执行角色:           postgres（超级用户，具备 CREATE EXTENSION 权限）
结论:                   扩展包已具备，不需要更换镜像，迁移中直接创建即可
```

```sql
-- 必须放在所有 vector 列/索引之前
CREATE EXTENSION IF NOT EXISTS vector;
```

**a) entity_registry 改造为 entities 形态**（保留原表名或重命名均可，本文按 `entity_registry` 表述）：

```sql
-- 收窄 domain 到 5 类（先清理不合规数据，见 2.2.2）
ALTER TABLE rag_mvp.entity_registry
    DROP CONSTRAINT IF EXISTS entity_registry_domain_chk;
ALTER TABLE rag_mvp.entity_registry
    ADD CONSTRAINT entity_registry_domain_chk CHECK (
        domain IN ('organization', 'person', 'project', 'policy', 'contract')
    );

-- 新增实体语义与统计字段
ALTER TABLE rag_mvp.entity_registry
    ADD COLUMN IF NOT EXISTS keywords TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS embedding vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128),
    ADD COLUMN IF NOT EXISTS embedding_dimensions INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_mentioned_at TIMESTAMPTZ;

-- 索引：HNSW 优于 IVFFlat，实体持续新增
CREATE INDEX IF NOT EXISTS entity_registry_embedding_hnsw_idx
    ON rag_mvp.entity_registry USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS entity_registry_name_trgm_idx
    ON rag_mvp.entity_registry USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entity_registry_normalized_trgm_idx
    ON rag_mvp.entity_registry USING gin (normalized_key gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entity_registry_keywords_gin_idx
    ON rag_mvp.entity_registry USING gin (keywords);
```

候选表的 domain CHECK 需单独处理（已核对 live schema）：

```sql
-- entity_candidates 已有 entity_candidates_domain_chk，需要先删再按 5 类重建
ALTER TABLE rag_mvp.entity_candidates
    DROP CONSTRAINT IF EXISTS entity_candidates_domain_chk;
ALTER TABLE rag_mvp.entity_candidates
    ADD CONSTRAINT entity_candidates_domain_chk CHECK (
        candidate_domain IN ('organization', 'person', 'project', 'policy', 'contract')
    );

-- 注意：entity_aliases 当前没有 domain 约束，这里是"新增"而不是"收窄"
ALTER TABLE rag_mvp.entity_aliases
    ADD CONSTRAINT entity_aliases_domain_chk CHECK (
        domain IN ('organization', 'person', 'project', 'policy', 'contract')
    );
```

**b) chunk_branches 改造为 chunk_mounts 形态**：

```sql
-- 1) 先删掉与挂载语义重复的旧字段及其约束
--    现状：match_method(exact/alias/confirmed) + match_score 与新增的
--    mount_method + confidence 语义重叠，保留会出现两套来源与两套分数
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_method_chk;
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_score_chk;
ALTER TABLE rag_mvp.chunk_branches
    DROP COLUMN IF EXISTS match_method;
ALTER TABLE rag_mvp.chunk_branches
    DROP COLUMN IF EXISTS match_score;

-- 2) 新增挂载语义字段（直接带默认值，避免 UPDATE + SET NOT NULL 两步）
ALTER TABLE rag_mvp.chunk_branches
    ADD COLUMN IF NOT EXISTS confidence NUMERIC(5,4) NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS mount_method VARCHAR(32) NOT NULL DEFAULT 'explicit';

ALTER TABLE rag_mvp.chunk_branches
    ADD CONSTRAINT chunk_branches_mount_method_chk CHECK (
        mount_method IN ('explicit', 'window_batch', 'llm_infer')
    ),
    ADD CONSTRAINT chunk_branches_confidence_chk CHECK (
        confidence >= 0 AND confidence <= 1
    );

-- 3) 删除 branch_key 与 tree_nodes 外键
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_tree_node_fk;
ALTER TABLE rag_mvp.chunk_branches
    DROP COLUMN IF EXISTS branch_key;

-- 4) 主键从 (chunk_id, branch_key) 改为 (chunk_id, entity_id)
ALTER TABLE rag_mvp.chunk_branches
    DROP CONSTRAINT IF EXISTS chunk_branches_pkey;
ALTER TABLE rag_mvp.chunk_branches
    ADD PRIMARY KEY (chunk_id, entity_id);
```

`chunk_branches` 当前 0 行（已核对），删列不会丢数据。

**c) 新增 entity_relations**：

```sql
CREATE TABLE IF NOT EXISTS rag_mvp.entity_relations (
    relation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type VARCHAR(16) NOT NULL,
    scope_id UUID NOT NULL,
    source_entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    target_entity_id UUID NOT NULL REFERENCES rag_mvp.entity_registry(id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL,
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0.8,
    evidence_chunk_ids CHAR(64)[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_relations_scope_type_chk CHECK (scope_type IN ('organization', 'user')),
    CONSTRAINT entity_relations_type_chk CHECK (relation_type IN (
        'works_for', 'participates_in', 'belongs_to', 'governed_by',
        'signed_by', 'related_to', 'applies_to', 'contacts'
    )),
    CONSTRAINT entity_relations_confidence_chk CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_relations_key_uq
    ON rag_mvp.entity_relations (scope_type, scope_id, source_entity_id, target_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS entity_relations_source_idx
    ON rag_mvp.entity_relations (scope_type, scope_id, source_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS entity_relations_target_idx
    ON rag_mvp.entity_relations (scope_type, scope_id, target_entity_id, relation_type);
```

注意 `evidence_chunk_ids` 用 `CHAR(64)`，与 `chunks.chunk_id` 的 sha256 格式一致，不用 UUID。

**d) 删除 tree_nodes**：

```sql
DROP TABLE IF EXISTS rag_mvp.tree_nodes;
```

管理端树接口改为从 `entity_registry GROUP BY domain` 派生，不再依赖物化节点表。

**e) 迁移执行顺序**：扩展创建（0）→ 表与字段改造（a、b、c）→ 删除 tree_nodes（d）。顺序不可调换，否则 vector 列与 tree_nodes 外键会报错。

#### 2.2.2 数据处置

2026-10-09 实测目标库现有数据量：

| 表 | 行数 | 处置 |
|---|---|---|
| `rag_mvp.chunks` | 1662 | **保留** |
| `rag_mvp.resource_snapshots` | 有数据 | **保留** |
| `rag_mvp.outbox_events` | 3166 | **保留**（事件流，不属树侧数据） |
| `rag_mvp.tree_nodes` | 45 | 清空（与 chunk_branches=0 不一致，属陈旧派生数据） |
| `rag_mvp.chunk_branches` | 0 | 清空（已是空表） |
| `rag_mvp.entity_registry` | 0 | 清空（已是空表） |
| `rag_mvp.entity_aliases` | 0 | 清空（已是空表） |
| `rag_mvp.entity_candidates` | 35 | 清空（正则发现的自动候选，将由 LLM 扫描重新生成） |
| `rag_mvp.entity_candidate_mentions` | 73 | 清空（同上） |
| `rag_mvp.entity_review_requests` | 0 | 清空（无人工审核记录） |
| `rag_mvp.branch_refresh_jobs` | 0 | 清空（已是空表） |

结论：**当前没有需要保留的人工审核成果**（registry / aliases / review_requests 均为空），因此不执行种子导出，直接按新流程重建即可。

```yaml
保留:
  - rag_mvp.chunks（解析与 embedding 成本最高）
  - rag_mvp.resource_snapshots
  - rag_mvp.outbox_events
  - Elasticsearch 中的 chunk 正文与向量

清空:
  - rag_mvp.tree_nodes
  - rag_mvp.chunk_branches
  - rag_mvp.entity_registry
  - rag_mvp.entity_aliases
  - rag_mvp.entity_candidates
  - rag_mvp.entity_candidate_mentions
  - rag_mvp.entity_review_requests
  - rag_mvp.branch_refresh_jobs
```

**回滚边界（重要）**：`down` 迁移只能还原表结构，**无法恢复被清空的实体、候选与挂载数据**。数据恢复依赖按第 4 步重新回填。决策与发布评审时必须以此为前提，不要把它当成可完整回退的常规迁移。

执行顺序：

```text
1. 应用新 schema（迁移）
2. 清空树侧表
3. 导入种子实体（可选）
4. 显式匹配回填 chunk_branches（mount_method = explicit）
5. 触发 branch_refresh 全量刷新，把 entity_ids / entity_mounts 写回 ES
6. 校验检索能力（传统路径不回归）
```

其中第 5 步复用现有 `BranchRefreshService`，但需把回写内容从 `branch_keys` 改成 `entity_ids` / `entity_mounts`。

#### 2.2.3 ES mapping 变更

`services/rag/config/elasticsearch/rag-chunks-index-v1.json` 增加：

```json
"entity_ids": { "type": "keyword" },
"entity_mounts": {
  "type": "nested",
  "properties": {
    "entity_id": { "type": "keyword" },
    "domain": { "type": "keyword" },
    "confidence": { "type": "float" },
    "method": { "type": "keyword" }
  }
}
```

说明：

- 新增字段属于 additive mapping 变更，**不需要新建索引**；存量文档由 mount 刷新通道回写。
- `branch_keys` 字段保留在 mapping 中不删（避免强制 reindex），但代码停止读写。
- 若索引确需重建，走既有 `projection_records` / 索引重建流程，不在本阶段手工操作。

### 2.3 任务清单

#### Week 1: 迁移与数据处置

- [x] **Day 1-2**: 编写并评审迁移脚本
  - [x] `20261009_tree_rag_v2_schema.up.sql` / `.down.sql`（计划原写的 20261008 与实际文件名不符，已改正）
  - [x] domain 收窄前的脏数据清理
  - [x] `chunk_branches` 主键与字段改造
  - [x] `entity_relations` 建表
  - [x] 删除 `tree_nodes`
- [x] **Day 3**: 本地与测试环境演练
  - [x] 正向迁移
  - [x] 回滚迁移
  - [x] 校验无残留外键
- [x] **Day 4**: 数据处置
  - [x] 导出可选种子实体（实测无人工作成果，未执行导出）
  - [x] 清空树侧表
  - [x] 确认 chunks 与 ES 数据完好
- [x] **Day 5**: ES mapping 变更
  - [x] 更新 index mapping 配置
  - [x] 验证新增字段可写

#### Week 2: 重建与验证

- [x] **Day 1-2**: mount 回填
  - [x] 改造 `BranchRefreshService` 输出 `entity_ids` / `entity_mounts`
  - [x] 分批回填，避免一次全量
- [x] **Day 3**: 检索回归
  - [x] 传统全库检索结果与迁移前一致
  - [x] tree_mode=off 下无异常（另见 树形RAG-回滚演练-交付文档.md）
- [x] **Day 4**: 管理端接口适配
  - [x] entity-tree 改为派生实现
  - [x] entity-candidates 审核链路可用
- [x] **Day 5**: 阶段验收与文档
  - [x] 更新 Schema 文档
  - [x] 更新部署说明（pgvector 依赖）

### 2.4 验收标准

```yaml
结构验收:
  ✅ domain CHECK 已收窄到 5 类
  ✅ tree_nodes 已删除，无残留外键引用
  ✅ chunk_branches 无 branch_key 列，主键为 (chunk_id, entity_id)
  ✅ entity_relations 表已建立且带 scope 字段
  ✅ entity_registry 已具备向量与统计字段
  ✅ 迁移可正向执行、可回滚

数据验收:
  ✅ chunks / resource_snapshots / ES 正文与向量完好
  ✅ 树侧表已清空
  ✅ mount 回填完成，mount_method 全部为 explicit

能力验收:
  ✅ tree_mode=off 时检索结果与迁移前一致
  ✅ 管理端 entity-tree / entity-candidates 接口可用
  ✅ ES 文档已带 entity_ids / entity_mounts
```

### 2.5 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| 脏数据违反新 domain CHECK | 中 | 高 | 迁移前先统计并清理 asset/location/unclassified 数据 |
| 全量 mount 回填压垮数据库/ES | 中 | 中 | 分批回填 + 限速 + 水位线 |
| pgvector 扩展未启用 | 低 | 中 | 已实测扩展包存在（vector 0.8.5），迁移开头 CREATE EXTENSION 即可 |
| 回滚不完整导致无法恢复 | 低 | 高 | down 脚本必须在测试环境完整演练后再上生产 |

## 3. Phase 1: 定位管线与检索接入（Week 3-5）

### 3.1 目标

```text
实现 L0-L5 实体定位管线并接入检索，让 tree 模式真正生效，
同时保留 shadow 模式用于对照和回滚。
```

详细接口契约见《实体定位五层管线接口草案》，本节只列交付物、任务和验收。

### 3.2 交付物

#### 3.2.1 五层定位管线

新增 `services/rag/app/application/entity_locator.py`，实现：

```text
L0  mention 抽取（最长优先、互不重叠、指代识别）
L1  精确匹配（canonical + alias，返回多条，不加 LIMIT 1）
L2  模糊匹配（pg_trgm / 编辑距离 / 包含前缀，三条并列规则）
L3  语义匹配（pgvector ANN，scope 硬过滤，type/recency/keywords 软加权）
L4  LLM 门控验证（仅低置信、指代、多候选取近时触发）
L5  汇总（范围合成 AND/OR + 残查询改写 + 降级分支）
```

关键约束：

- 定位粒度是 mention，不是整条 query；L1 命中不得阻断其他 mention。
- **只有 scope 是硬过滤**，type / recency / keywords 只做排序加权。
- 每层带 `match_method`，置信度不可跨方法比较或相加。
- 输出 `EntityScope { entity_ids, composition, min_mount_confidence, residual_query, unresolved }`。

#### 3.2.2 实体向量与回填任务

```text
- 复用现有 EmbeddingProvider，对实体文本生成向量：
  entity_text = canonical_name + aliases + description + keywords
- 新增实体向量回填任务：扫描 embedding_status='pending' 的实体
- 实体创建 / 改名 / 合并后置为 pending，由任务回填
- L3 必须容忍 embedding IS NULL
```

扩展名为业务事件时，向量重建不阻塞检索。

#### 3.2.3 检索服务接入

改造 `RAGRetrievalService`：

```text
改动点:
  - _resolve_branches 改为调用 EntityLocator，产出 entity_ids + min_mount_confidence
  - ES 过滤从 branch_keys 前缀改为 entity_ids terms + entity_mounts nested 阈值
  - 检索语句使用 residual_query，而不是原始 query
  - tree_mode 语义: off / shadow / tree
      off    : 不定位，纯传统检索
      shadow : 照常定位并记录诊断，但不影响结果
      tree   : 命中实体则以节点范围为主；未命中降级全库
  - diagnostics 增加 resolved_entity_count / resolved_branch_count /
    locate_latency_ms / llm_invoked / fallback_reason
```

两个必须显式定义的边界：

```text
1. residual_query 为空时（例如查询本身就是实体名"A项目"）：
   - 不能拿空串去做 BM25 / kNN
   - 回退顺序：残查询为空 → 用原 query；原 query 仍无有效词 → 跳过文本匹配，
     仅按实体范围返回，由后续排序决定

2. Agent 传入 entity_ids 时：
   - 必须逐个校验属于当前 scope_type / scope_id 且 status='active'
   - 校验失败按"未传入"处理并回退到 L0-L4，不能直接信任外部传入的 ID
```

请求契约增加字段（见接口草案 9.2）：

```text
entity_ids             Agent 规划阶段已定位的实体，传入则跳过 L0-L4
entity_composition     and / or
min_mount_confidence   严格 0.85 / 探索 0.65
locate_allow_llm       是否允许 L4
```

#### 3.2.4 审核链路：接口复用，界面新建

**后端接口与数据模型直接复用，不新建：**

```text
entity_candidates / entity_candidate_mentions   候选与证据
entity_review_requests                          幂等审核
POST /admin/entity-candidates/{id}/review       promote / merge / ignore / defer
branch_refresh_jobs                             审核后增量刷新
```

需要改的只有一处：候选来源从 `memory_service` 的正则发现，改为窗口 LLM 提取（Phase 2 完成）。Phase 1 期间保持正则来源可用，作为过渡。

**前端审核界面需要新建**，这是 Phase 2 的前置依赖：窗口扫描一旦产出候选，没有审核通道就无处落地，树也就始终为空。范围守最小：

```text
要做:
  - 候选列表：按提及次数与评分排序，支持类型筛选与分页
  - 详情抽屉：置信度、提及次数、证据 chunk、LLM 理由、建议别名
  - 四个动作：promote / merge / ignore / defer
  - 批量 promote 与 ignore
  - 提交携带 review_request_id，复用接口幂等

不做:
  - "试用节点"（接口 07 无此状态，按实体灰度属 Phase 4）
  - 树可视化
  - 图谱式合并选择器（先用搜索框 + 下拉）
```

审核界面同时是质量度量入口：审核时长与批准率需要从该页面埋点采集，否则 Phase 2 的两项验收指标无法测量。

### 3.3 任务清单

#### Week 3: 定位管线

- [x] **Day 1-2**: L0 + L1 + L2
  - [x] mention 抽取与位置信息
  - [x] 精确匹配（多条返回）
  - [x] 模糊匹配三条规则
  - [x] 单元测试（含同名跨 domain、多 mention）
- [x] **Day 3-4**: L3 语义匹配
  - [x] pgvector 查询与 over-fetch
  - [x] 软信号加权（type + recency 已实现，见 树形RAG-L3软信号recency-交付文档.md；**keywords 未实现——该列被读但无人写**）
  - [x] 空向量容忍
- [x] **Day 5**: L4 门控验证（见 树形RAG-L4门控验证-交付文档.md）
  - [x] 触发条件实现
  - [x] 候选集校验与超时降级

#### Week 4: 向量与检索接入

- [x] **Day 1-2**: 实体向量回填
  - [x] EmbeddingProvider 复用
  - [x] 回填任务与状态管理
  - [x] 增量触发（实体写回时 `embedding_status` 重置为 pending）
- [x] **Day 3-4**: 检索接入
  - [x] ES 过滤改造
  - [x] residual_query 接入
  - [x] tree_mode 三态
- [x] **Day 5**: 诊断字段与日志

#### Week 5: 联调、测试与灰度

- [x] **Day 1-2**: 集成测试与性能测试
  - [x] 五层管线端到端
  - [x] 分层延迟测量（见 树形RAG-定位分层耗时-交付文档.md）
  - [x] L4 调用率统计
- [x] **Day 3**: 灰度准备
  - [x] 灰度开关与用户白名单（见 树形RAG-灰度白名单-交付文档.md）
  - [x] 回滚预案演练（见 树形RAG-回滚演练-交付文档.md）
- [x] **Day 4-5**: 灰度观察
  - [x] shadow 模式对照
  - [x] 指标分析
  - [x] Bug 修复

#### Week 5（前端并行）: 审核页启动

- [x] **Day 1-2**: 旧 mock 处理
  - [x] 摘除 `/organization/knowledge` 导航入口，或加"未接入"标识（组织页入口已摘除，旧页面顶部保留"设计稿演示"告示，仅 URL 直达）
  - [x] 抽取可复用的组件与样式（候选抽屉字段组织、对照表）
- [x] **Day 3-5**: 审核页骨架
  - [x] 候选列表接 `GET /api/v1/admin/entity-candidates`
  - [x] 详情抽屉接 `GET /api/v1/admin/entity-candidates/{id}`
  - [x] 四个动作接 `POST .../review`（携带幂等键）

### 3.4 验收标准

延迟指标必须按路径拆开，不能只写一个数字：

```yaml
功能验收:
  ✅ L0-L5 管线可端到端运行
  ✅ 多 mention 查询能同时定位多个实体
  ✅ 无实体命中时正确降级全库检索
  ✅ tree_mode=shadow 不影响结果，仅记录诊断
  ✅ tree_mode=tree 生效且可一键回滚

性能验收（常规路径，L4 未触发）:
  ✅ 实体定位 p95 <= 80ms
  ✅ 端到端检索 p95 <= 150ms

性能验收（升级路径，L4 触发）:
  ✅ L4 超时严格 <= 800ms
  ✅ L4 调用率 <= 语义查询的 20%
  ✅ 升级路径延迟单独统计，不并入常规 p95

质量验收:
  ✅ 实体定位召回率基于标注集评估（目标值 Phase 1 定基线）
  ✅ 降级率（no_entity_match）有监控且不高于预期

安全验收:
  ✅ 跨 scope 实体与 chunk 不可见（越权检查用例通过）
```

### 3.5 灰度策略

```yaml
阶段1（Week 5 Day 4）:
  范围: 1 个内部用户
  tree_mode: shadow
  目标: 验证管线完整性与诊断数据

阶段2（Week 5 Day 5 - Week 6）:
  范围: 2-3 个早期用户
  tree_mode: shadow
  目标: 真实数据下评估定位质量

阶段3（Week 7+）:
  范围: 逐步扩到 10 个用户
  tree_mode: tree
  目标: 验证检索效果提升
```

灰度优先按**查询类型**切分（例如只对含明确实体的查询启用 tree 模式），比单纯按用户切更容易定位问题。

落地方式（见 树形RAG-灰度白名单-交付文档.md）：

```text
RAG_TREE_MODE            部署默认模式，off / shadow / tree
RAG_TREE_ROLLOUT_SCOPES  提升名单：user:<id>,organization:<id>
RAG_TREE_SHADOW_SAMPLE_RATE  shadow 定位的采样率，按 scope+query 哈希决定
```

`off` 是硬杀开关，压过白名单——否则回滚预案第 2 步切 off 之后，被放量的那批 scope
反而停不下来。"按查询类型切分"由管线本身承担：tree 模式下定位不到实体时直接退化为
traditional，等于只对含明确实体的查询生效。

### 3.6 回滚预案

```yaml
触发条件:
  - 检索结果明显劣化
  - 跨 scope 越权
  - 定位延迟超预算且无法快速修复

回滚步骤:
  1. tree_mode 切回 shadow（保留诊断，不影响结果）
  2. 仍异常则切 off（纯传统检索）
  3. 关闭实体向量回填任务
  4. 观察指标恢复

数据保留:
  - 不删除实体、mount 与向量数据
  - ES 新增字段保留，不影响旧路径
```

## 4. Phase 2: 窗口挂载与质量体系（Week 6-8）

### 4.1 目标

```text
把隐式主题消息挂载到实体节点，建立置信度分级与评估体系，
让召回质量可度量、可优化。
```

### 4.2 交付物

#### 4.2.1 窗口 LLM 提取与挂载

```text
新增 EntityWindowScanWorker:
  - 按会话分组，滑动窗口 size=20 / step=10（50% 重叠）
  - 调用 LLM 一次产出实体 + 关系
  - 已匹配到 active 实体 → 直接挂载 chunk（window_batch）
  - 未匹配的新名字 → 写入 entity_candidates 走人工审核
  - 关系写入 entity_relations（供 Phase 3 使用）
```

这里要明确一条分工，避免自动化与审核打架：

```text
已知实体  → 自动挂载（带置信度）
未知实体  → 进候选审核，审核通过后再挂载历史 mention
```

挂载写入必须满足两条规则（step=10 有 50% 重叠，同一条消息会被相邻两个窗口处理到）：

```text
1. 幂等：以 (chunk_id, entity_id) 为键 upsert，重复处理不产生第二条挂载
2. 置信度合并：重复命中时 confidence = GREATEST(已存, 新值)，
   不允许低分覆盖高分；mount_method 按
   explicit > window_batch > llm_infer 的优先级保留更强的来源
```

#### 4.2.2 置信度分级

```text
explicit      0.95  消息明确提到实体
window_strong 0.85  窗口主题明确，消息相关性强
window_weak   0.65  窗口主题明确，消息相关性弱
llm_infer     0.50  LLM 推断的隐含关系
```

检索侧按场景取阈值：严格场景（表单填写、联系方式）用 `>= 0.85`，探索场景用 `>= 0.65`。通过 `min_mount_confidence` 参数下发。

#### 4.2.3 评估集与指标看板

```text
- 构建标注查询集：查询、正确实体、正确 chunk（用于定位与检索双层评估）
- 指标接入现有 search_events / diagnostics 链路
- Grafana 看板：定位分层延迟、L4 调用率、挂载置信度分布、
  降级率、审核吞吐
```

#### 4.2.4 模型调用参数与并发（实测基线）

2026-10-09 对抽取模型 `deepseek-flash`（`https://best-api.huggingclaw.store/v1`）实测：

| 配置 | 延迟 | completion | reasoning |
|---|---|---|---|
| 默认 | 10.3s | 1823 | 1741 |
| `reasoning_effort=low` | 5.6s | 1125 | 1087 |
| `reasoning_effort=minimal` | 2.7s | 440 | 402 |
| **`thinking={"type":"disabled"}`** | **1.3s** | **66** | **0** |

结论与硬性要求：

```text
1. 默认使用 thinking={"type":"disabled"}：推理 token 归零，延迟降约 8 倍、token 降约 10 倍；
   真实 20 条窗口实测 2.2~4.0s。不要把 reasoning_effort=minimal 当作回退档：
   长窗口上它会把 max_tokens 预算耗在推理上并导致 JSON 截断（实测一次打满 4000 token）；
   若关闭推理确实不达标，改用更大的 max_tokens 配合 low 档。
2. max_tokens >= 2000。实测 max_tokens=800 时推理吃满预算，正文静默返回空串且不报错。
3. 必须显式判断 content 非空，为空按调用失败重试；禁止落库为"窗口无实体"。
4. relation.type 必须按 8 个枚举值校验，不在枚举内直接丢弃。
   实测：prompt 未写枚举时模型返回 discusses，补上枚举规则后返回 participates_in。
5. 并发初值 8~16，上限 32。实测 16 并发延迟平稳（约 1.8s），
   64 并发 p50 由 2.0s 升到 3.4s，全程未出现 429。
6. 实体名称与 evidence 必须过滤凭据类信息（密码 / token / 密钥 / 验证码 / 长数字串），
   并在写入 entity_candidates.sample_context 前脱敏。
   实测真实窗口把"数据库账号root 密码123456"当成了实体，此路径不能放开。
```

真实 20 条窗口实测（6 个窗口取自库内真实群聊 / 私聊，prompt 803~2444 字符）：

| 配置 | 延迟 | prompt | completion | JSON 通过 | 实体数 | 关系数 |
|---|---|---|---|---|---|---|
| `thinking=disabled` | 2.2 ~ 4.0s | 434 ~ 1104 | 134 ~ 364 | 6/6 | 3~5 | 0~4 |
| `reasoning_effort=minimal` | 3.8 ~ 26.1s | 475 ~ 1128 | 287 ~ 4000 | 5/6 | 0~5 | 0~1 |

关闭推理在真实窗口上不仅更快，实体与关系产出也更多，且没有一次被截断。

TPM 压测（真实 20 条窗口，约 1370 token/次）：

| 并发 | wall | p50 | 状态码 | tokens | 估算 TPM |
|---|---|---|---|---|---|
| 8 | 2.78s | 2.56s | 全 200 | 10,960 | 约 237k |
| 16 | 2.73s | 2.56s | 全 200 | 21,488 | 约 472k |
| 32 | 3.30s | 2.73s | 全 200 | 43,623 | 约 793k |

结论：32 并发下实测约 79 万 token/min，未出现 429。按 600 会话/小时
（约 800 窗口 ≈ 110 万 token/小时 ≈ 1.8 万 token/min）计算，TPM 有约 40 倍余量，
**不构成瓶颈**。该网关不返回 `x-ratelimit-*` 头，以上均为实测推断值。

并发度推算（按真实窗口 3.0s/次、留 3x 余量）：

| 活跃会话/小时 | 窗口数/小时 | 串行耗时 | 建议并发 |
|---|---|---|---|
| 300 | 约 380 | 约 19 分钟 | 2~4 |
| 600 | 约 800 | 约 40 分钟 | 4~8 |

仍需注意：

```text
- 实测样本为 6 个窗口、3 个会话，上量后应按周抽样复核关闭推理的召回质量
- 5 类 domain 的已知副作用：服务器/设备类对象无归属类型，
  实测"116服务器"被归为 project，属可接受的临时降级
```

#### 4.2.5 审核界面（承接 Phase 1）

窗口扫描在本阶段产出候选，审核界面必须同期可用，否则候选积压、树无法增长：

```text
Week 6: 审核页可用（列表 + 详情 + 四个动作 + 类型筛选）
Week 7: 批量操作 + 审核埋点（审核时长、批准率、拒绝率）
Week 8: 审核效率优化（快捷键、默认名称推荐、历史记录填充）
```

验收闭环：候选 → `promote` → 树内出现实体 → 检索命中该实体范围。这条闭环不通，Phase 2 不算完成。

### 4.3 任务清单

#### Week 6: 窗口扫描

- [ ] **Day 1-2**: 滑动窗口与分组
  - [ ] 按会话分组、窗口切分
  - [ ] 处理进度水位线（不依赖布尔标记）
- [ ] **Day 3-4**: LLM 提取
  - [ ] Prompt 设计与结构化输出校验（含 8 类关系枚举）
  - [ ] thinking=disabled + max_tokens>=2000 + 空正文重试
  - [x] 结果缓存与限流（见 树形RAG-提取缓存与限流-交付文档.md）
- [ ] **Day 5**: 挂载写入
  - [ ] 已知实体自动挂载
  - [ ] 未知实体写候选
- [ ] **Day 3-5（前端并行）**: 审核页可用
  - [ ] 列表 + 详情 + 四个动作 + 类型筛选

#### Week 7: 置信度与候选链路

- [ ] **Day 1-2**: 置信度分级落地
  - [ ] mount_method / confidence 写入
  - [ ] 检索阈值参数
- [ ] **Day 3-4**: 候选来源切换
  - [x] memory_service 正则来源切到 LLM 来源（见 树形RAG-候选来源切换-交付文档.md）
  - [ ] 审核接口回归
- [ ] **Day 5**: 关系写入
  - [ ] entity_relations 落库
- [ ] **Day 3-5（前端并行）**: 批量操作与审核埋点
  - [x] 批量 promote / ignore（审核页多选后逐条调用审核接口，各自带幂等键，并按成功/失败分别回报；不是单次服务端批量接口）
  - [x] 批准率、拒绝率与审核吞吐（见 树形RAG-审核吞吐指标-交付文档.md）
  - [ ] 审核时长（需审核页上报停留时长 + 一次迁移；当前无审核记录可校准）

#### Week 8: 评估与看板

- [ ] **Day 1-2**: 标注查询集
- [x] **Day 3-4**: 指标与看板
  - [x] 无标注指标与 `/metrics` 端点（见 树形RAG-指标暴露与口径修正-交付文档.md）
  - [x] Prometheus 抓取配置与告警规则（见 树形RAG-监控抓取与告警规则-交付文档.md）
  - [ ] Grafana 面板与监控栈（需先决定是否在本仓库引入 Prometheus/Grafana）
  - [x] 真实 20 条消息窗口的延迟与 TPM 复测（见 树形RAG-窗口扫描性能实测-交付文档.md）
  - [x] 并发度定档（初值 8~16，上限 32）
- [ ] **Day 5**: 阶段评估与调优

### 4.4 验收标准

```yaml
功能验收:
  ✅ 窗口扫描可定时运行且可断点续跑
  ✅ 隐式主题消息能被挂载
  ✅ 未知实体进入候选而非直接入树
  ✅ 关系数据正确落库
  ✅ 空正文响应按调用失败重试，不落库为"无实体"
  ✅ 关系类型 100% 落在 8 个枚举内
  ✅ 凭据类信息（密码 / token / 密钥 / 验证码）不进入实体与候选证据
  ✅ 审核闭环可用：候选 -> promote -> 树内出现实体 -> 检索命中该实体范围

质量验收:
  ✅ 挂载准确率 > 85%（人工抽样）
  ✅ 人工批准率 > 80%
  ✅ 平均审核时间 < 30 秒/实体（由审核页埋点采集）
  ✅ 置信度分布可见，低置信占比可解释

性能与成本验收:
  ✅ 窗口扫描不影响在线检索延迟
  ✅ 建议并发（8~16）下能在一小时内处理完当日窗口量
  ✅ 关闭推理后单次提取延迟 p95 <= 5s（真实窗口复测确认）
  ✅ LLM 成本在预算内（< $30/月）

观测验收:
  ✅ 定位与检索指标接入看板
  ✅ 降级率可查
```

## 5. Phase 3: 关系网络（Week 9-10）

### 5.1 目标

```text
让关系数据在检索侧真正生效，支持“横向查找”类查询，
例如“张三参与了哪些项目”“A公司下属项目”。
```

Phase 2 已经把关系写入 `entity_relations`，本阶段做查询侧扩展与质量治理。

### 5.2 交付物

```text
1. 关系扩展检索
   - 实体定位后，若节点内 chunk 不足（< top_k * 0.5）
   - 沿关系做 1 跳扩展，置信度 >= 0.7，最多展开 3 个相关实体
   - 扩展结果标记 source=related 并参与 RRF
   - 严格限制遍历深度为 1，禁止无界图遍历

2. 关系查询接口
   - 按 (entity, relation_type, direction) 查询相关实体
   - 支持 outbound / inbound / both

3. 关系质量治理
   - 低置信度关系丢弃或降权
   - 证据 chunk 保留，用于人工抽查
   - 合并实体时同步修正关系端点
   - 重复识别时的合并规则：confidence = GREATEST(已存, 新值)，
     evidence_chunk_ids 取并集，避免唯一约束导致置信度被覆盖降分
```

### 5.3 任务清单

- [ ] **Week 9 Day 1-2**: 关系查询接口
- [ ] **Week 9 Day 3-4**: 关系扩展检索接入
- [ ] **Week 9 Day 5**: 深度/数量限制与防环
- [x] **Week 10 Day 1-2**: 关系质量与证据（见 树形RAG-关系证据链-交付文档.md；证据链已端到端可读，**关系准确率标注仍待人工**）
- [ ] **Week 10 Day 3-4**: 评估与调优
- [ ] **Week 10 Day 5**: 阶段验收

### 5.4 验收标准

```yaml
功能验收:
  ✅ 关系类查询（"X 参与的 Y"）可正确召回
  ✅ 关系扩展限制为 1 跳，无性能退化
  ✅ 合并实体后关系端点一致

质量验收:
  ✅ 关系提取准确率 > 80%（人工抽样）
  ✅ 关系扩展带来的召回提升可量化

性能验收:
  ✅ 关系查询延迟 p95 <= 50ms
```

## 6. Phase 4: 自动化升级（Week 11+，持续）

### 6.1 目标

```text
在质量与指标稳定后，逐步减少人工审核工作量。
```

Phase 4 的所有动作都以 Phase 2/3 的评估数据为前提，不提前排期到 Week 10 之前。

### 6.2 功能规划

#### 6.2.1 自动合并建议

```yaml
功能: 识别可能重复的实体，生成建议，人工确认
前置: 实体量足够、监督信号来自审核记录
指标: 建议准确率 > 80%，采纳率 > 60%
```

#### 6.2.2 智能消歧

```yaml
功能: 处理“小张”这类不确定指代，持续观察并关联
注意: 需要新增不确定状态，会再次修改 entity_registry.status 约束，
      因此必须与 domain 收窄迁移解耦，单独一个迁移文件
指标: 消歧准确率 > 75%，未确定实体比例 < 15%
```

#### 6.2.3 部分自动批准

```yaml
功能: 学习人工审核模式，高置信度实体自动批准
指标: 自动批准准确率 > 95%，人工工作量下降 50%
```

### 6.3 长期演进

```text
- 实体属性自动补全
- 实体图谱可视化
- 时间线视图
- 模型微调与 Prompt 工程
- 性能与成本优化
```

## 7. 关键指标体系

### 7.1 北极星指标

```yaml
核心指标: 树形检索相对传统 RAG 的检索质量提升
定义:
  - 基于标注集的召回 / 相关性提升
  - 前提是不显著劣化延迟与成本
目标:
  - Phase 1: 建立基线
  - Phase 2: 提升 10%+
  - Phase 3: 提升 20%+
测量: 标注集评估 + A/B 对照
```

### 7.2 质量指标

| 指标 | Phase 1 | Phase 2 | Phase 3 | 测量方法 |
|---|---|---|---|---|
| 实体定位召回率 | 建基线 | > 90% | > 92% | 标注查询集 |
| 实体定位准确率 | 建基线 | > 85% | > 88% | 标注查询集 |
| 挂载准确率 | - | > 85% | > 88% | 人工抽样 |
| 关系提取准确率 | - | - | > 80% | 人工抽样 |
| 树形检索相关性 | 建基线 | > 85% | > 88% | 标注集评估 |
| 挂载覆盖率 | 建基线 | 监控 | > 60% | 系统统计 |

### 7.3 效率指标

延迟必须按路径拆开统计，否则无法解释波动：

| 指标 | Phase 1 | Phase 2 | Phase 3 | 说明 |
|---|---|---|---|---|
| 实体定位延迟（常规路径 p95） | <= 80ms | <= 60ms | <= 50ms | L4 未触发 |
| 端到端检索延迟（常规路径 p95） | <= 150ms | <= 150ms | <= 120ms | |
| L4 调用延迟上限 | <= 800ms | <= 800ms | <= 600ms | 超时即降级 |
| L4 调用率 | <= 20% | <= 20% | <= 15% | 相对语义查询 |
| 降级率（no_entity_match） | 监控 | <= 30% | <= 25% | 语义查询中未定位比例 |
| 降级率（mount 失败） | < 5% | < 3% | < 2% | 技术失败 |
| 审核时间 | <= 30s/实体 | <= 20s/实体 | <= 15s/实体 | |

### 7.4 安全指标

| 指标 | 目标 | 说明 |
|---|---|---|
| 跨 scope 越权检出 | 0 例 | 实体、mount、chunk 均受 scope 约束 |
| 未授权 protected 正文泄漏 | 0 例 | 沿用现有授权链路 |
| 审核接口越权调用 | 0 例 | 复用 Core 组织能力检查 |

安全指标为 P0 验收项，任一不达标不得进入下一阶段。

### 7.5 成本指标

| 指标 | Phase 1 | Phase 2 | Phase 3 | 测量方法 |
|---|---|---|---|---|
| 月 LLM 成本 | < $20 | < $30 | < $40 | 费用统计 |
| 窗口扫描 LLM 调用量 | - | 受控 | 受控 | 调用计数 |
| 存储成本 | < 50GB | < 80GB | < 100GB | 数据库监控 |

### 7.6 监控看板

```yaml
业务面板:
  - 候选实体积压量
  - 每日新增 / 批准 / 拒绝
  - 树形检索命中率与降级率

性能面板:
  - 定位分层延迟（L0-L5）
  - L4 调用率与超时率
  - 端到端检索延迟（按 tree_mode 分组）
  - 数据库慢查询 / ANN 查询耗时

质量面板:
  - 挂载置信度分布
  - 实体定位方法分布（exact / alias / fuzzy / semantic / llm）
  - 关系数量与来源分布

告警规则:
  - 候选积压 > 100
  - L4 调用率 > 30%
  - 降级率（技术失败）> 5%
  - 检索 p95 > 500ms
  - 每日 LLM 成本超预算
  - 跨 scope 越权检查失败
```

## 8. 风险管理

### 8.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 | 应急预案 |
|---|---|---|---|---|
| pgvector 扩展未启用或权限不足 | 低 | 高 | 已实测扩展可用；迁移开头显式 CREATE EXTENSION 并确认执行角色权限 | 由 DBA 手工建扩展后重跑迁移 |
| 带过滤的 ANN 召回不足 | 中 | 中 | over-fetch + 必要时 iterative_scan | 调大 over-fetch 或改精确搜索 |
| L4 延迟击穿预算 | 中 | 高 | 门控触发 + 800ms 超时 + 降级 | 临时关闭 L4（allow_llm=false） |
| 窗口挂载引入噪音 | 中 | 中 | 置信度阈值 + 人工抽检 | 提高阈值，收紧挂载范围 |
| 全量回填压垮数据库/ES | 中 | 高 | 分批 + 限速 + 水位线 | 暂停回填，保留中间态 |
| 关系扩展产生环或爆炸 | 低 | 中 | 限制 1 跳 + 数量上限 | 关闭关系扩展开关 |

### 8.2 数据与业务风险

| 风险 | 概率 | 影响 | 缓解措施 | 应急预案 |
|---|---|---|---|---|
| 旧数据清理误删有用审核成果 | 中 | 高 | 清理前导出种子实体 | 从导出清单恢复 |
| 实体重复严重 | 中 | 中 | 合并工具 + 相似度检测 | 定期清理 |
| 用户不审核导致积压 | 中 | 中 | 批量操作 + 高价值优先 | 降低提取频率 |
| 跨 scope 数据泄漏 | 低 | 极高 | scope 硬过滤 + 越权用例 | 立即回滚 tree_mode=off |

### 8.3 项目风险

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| 破坏性迁移导致停产 | 低 | 极高 | 测试环境完整演练正向与回滚 |
| Phase 0 估时不足 | 中 | 中 | 把回填与 ES 变更单列，不与其他任务混排 |
| 评估集缺失导致指标不可验证 | 高 | 中 | Phase 0/1 即启动标注集构建 |

## 9. 资源与分工

```yaml
后端（1-2 人）:
  - 迁移与数据处置
  - 定位管线与检索接入
  - 窗口扫描 Worker
  - 关系网络

前端（1 人）:
  - Phase 1 后期：审核页新建（复用旧 mock 的组件与样式，不在旧页面上改造）
  - Phase 2：审核页批量操作与埋点、检索诊断面板
  - 树可视化不在本期范围

测试/全栈（1 人）:
  - 标注集构建
  - 集成与性能测试
  - 越权用例

外部依赖:
  - PostgreSQL + pgvector（远端已具备）
  - Elasticsearch（现有）
  - LLM（用于窗口提取与 L4 验证）
```

时间投入（参考，按 1 后端 + 1 前端 + 1 测试估）：

```yaml
Phase 0（2 周）:   后端 60h + 测试 30h
Phase 1（3 周）:   后端 100h + 测试 40h + 前端 30h（审核页骨架）
Phase 2（3 周）:   后端 80h + 测试 50h + 前端 60h（审核页完成 + 诊断面板）
Phase 3（2 周）:   后端 60h + 测试 20h
```

Phase 0 的估时比 v1 高，因为新增了数据处置与 ES mapping 两块必需工作。

## 10. 决策点与检查点

### 10.1 关键决策点

```yaml
决策点1: Phase 0 -> Phase 1
  时间: Week 2 结束
  标准:
    ✅ 迁移可正向执行且可回滚
    ✅ 检索能力无回归（tree_mode=off）
    ✅ 安全用例通过
  不通过: 延期，优先修复地基问题

决策点2: Phase 1 -> Phase 2
  时间: Week 5 结束
  标准:
    ✅ 常规路径 p95 <= 80ms
    ✅ L4 调用率 <= 20%
    ✅ 跨 scope 越权为 0
  不通过: 保持 shadow 模式，继续优化

决策点3: Phase 2 -> Phase 3
  时间: Week 8 结束
  标准:
    ✅ 挂载准确率 > 85%
    ✅ 标注集与看板就绪
    ✅ 人工审核量可承受
    ✅ 审核闭环跑通（候选 -> promote -> 树内实体 -> 检索命中）
  不通过: 继续优化提取质量

决策点4: Phase 3 -> Phase 4
  时间: Week 10 结束
  标准:
    ✅ 关系类查询有可量化提升
    ✅ 关系准确率 > 80%
    ✅ 成本在预算内
  不通过: 继续保持 1 跳扩展，暂不自动化
```

### 10.2 检查点

```yaml
每周检查:
  - 进度与阻塞
  - 分层指标达成情况
  - 风险清单更新

Phase 结束检查:
  - 验收标准逐条核对
  - 文档更新（Schema / API / 运维）
  - 下一阶段准入判断
```

## 11. 附录

### 11.1 术语表

| 术语 | 定义 |
|---|---|
| **实体(Entity)** | 从对话中提取的结构化对象（公司、人、项目等） |
| **mention** | 查询或对话中指向某个实体的一段文本，定位的最小粒度 |
| **挂载(Mount)** | chunk 与实体的关联关系，带置信度与来源方法 |
| **EntityScope** | 定位输出：实体集合 + 组合方式 + 残查询 + 置信度阈值 |
| **残查询** | 剥离实体名后的检索语句 |
| **L4** | LLM 门控验证层，只在低置信或多候选时触发 |
| **tree_mode** | 检索模式：off / shadow / tree |
| **mount_method** | 挂载来源：explicit / window_batch / llm_infer |

### 11.2 参考文档

```yaml
设计文档（均位于 services/rag/docs/）:
  - 树形RAG设计方案.md
  - 实体节点生成方案-MVP.md
  - 实体定位五层管线接口草案.md
  - interfaces/07-rag-admin-entity-tree-api.md

代码位置:
  - 迁移: db/migrations/
  - 领域模型: services/rag/app/domain/rag.py
  - 持久化: services/rag/app/infrastructure/persistence/mvp.py
  - 检索: services/rag/app/application/rag_service.py
  - 匹配: services/rag/app/application/entity_service.py
  - ES 映射: services/rag/config/elasticsearch/rag-chunks-index-v1.json
```

### 11.3 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2024-01-XX | 初版，按从零新建表编排（已存档为 `树形RAG实施计划-v1.md`） |
| v2.0 | 2026-10-08 | 改为原地改造现有表；收窄 5 类 domain；引入 pgvector 与五层定位管线；检索契约改为 entity_ids / entity_mounts；阶段与指标重构 |
| v2.1 | 2026-10-09 | 实测目标库：pgvector 包可用但未启用，扩展创建提到迁移最前；删除与 mount 语义重复的 match_method / match_score；补挂载与关系合并规则、残查询空值兜底、entity_ids scope 校验、回滚边界；挂载覆盖率指标 |
| v2.2 | 2026-10-09 | 新增 Phase 2「模型调用参数与并发（实测基线）」：thinking=disabled、max_tokens、空正文重试、关系枚举校验、并发度与补测项；同步任务清单与验收标准 |
| v2.3 | 2026-10-09 | 补齐真实 20 条窗口实测与 TPM 压测数据；修正回退档建议（minimal 会截断，改用 low + 更大 max_tokens）；新增凭据类信息过滤与脱敏要求 |
| v2.4 | 2026-10-09 | 新增 1.5 前端与评估工具优先级（测试集 > 审核页 > 诊断面板 > 树可视化）；修正"审核不新建功能"的表述为"接口复用、界面新建"；审核页纳入 Phase 1 后期与 Phase 2 交付物，并作为窗口扫描的前置依赖；补前端分工与工时、审核闭环验收 |
