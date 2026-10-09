# 树形RAG Phase 1 交付文档：定位管线与检索接入

- **阶段**: Phase 1 / 定位管线与检索接入
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 完成（前端审核界面已建）
- **关联文档**: 树形RAG实施计划.md (v2.4)、实体定位五层管线接口草案.md、树形RAG-Phase0-地基改造-交付文档.md

> **已过期的部分（2026-10-09 设计回调）**：本文 §2.4 描述的"tree 模式下实体范围通道
> 与全库通道同时进入 RRF 融合（tree-first + 全库兜底）"已被取代。现在通道策略由入口
> 决定，传统入口与树入口互不混合，服务端不再做内部回退。当前语义见
> 《树形RAG-入口隔离与设计回调-交付文档.md》。定位管线本身（L0-L5）不受影响。

## 1. 阶段目标

实现 L0-L5 实体定位管线并接入检索，让 tree 模式真正生效，同时保留 shadow
模式用于对照和回滚。

## 2. 交付物

### 2.1 领域类型（`app/domain/location.py`）

```text
EntityMention    L0 产物：surface_form / normalized_form / 位置 / type_hint / is_deictic
EntityCandidate  逐层候选：entity_id / domain / match_method / match_score
LocatedEntity    mention 的最终判定，带 verified 标记
EntityScope      entity_ids / composition / min_mount_confidence / residual_query / unresolved
LocateRequest    scope + query + 上下文消息 + 阈值 + allow_llm
LocateResult     entities + scope + diagnostics
```

### 2.2 定位管线（`app/application/entity_locator.py`）

| 层 | 实现 |
|---|---|
| L0 | mention 抽取：注册名与别名按最长优先、互不重叠匹配（保留原始偏移用于残查询改写）；指代短语（那个项目/上次说的等）；类型后缀候选；**整句回退**——没有任何 mention 时把整句作为一个候选，让 L3 有机会处理 `aims` 这类既非注册名也无类型后缀的缩写 |
| L1 | 精确匹配 canonical / alias，**返回全部同名跨 domain 的结果**，不取第一条 |
| L2 | pg_trgm 相似度（≥0.8，映射到 0.80~0.85）、编辑距离、包含匹配（独立标记为 `substring`，0.70） |
| L3 | pgvector ANN（HNSW，over-fetch 5 倍后收敛）；L2 置信度不足时不短路，而是与 L3 候选合并取高分 |
| L4 | 门控验证：指代、substring、top1 分数不足、top1/top2 差距过小才触发；只允许从候选集中选，超时或异常降级 |
| L5 | 范围合成（默认 and，含"或"则 or）、残查询改写、top-N 截断 |

### 2.3 实体向量回填（`app/application/entity_embedding_service.py`）

```text
实体创建时 embedding_status='pending'（迁移默认值），由本服务批量回填
嵌入文本 = canonical_name + description + keywords + aliases
  —— 别名必须参与：用户输入的 "aims" 只存在于别名里
provider 失败时保持 pending，下轮重试，不影响审核动作
merge 增加别名后把目标实体重新置为 pending（向量会变）
```

### 2.4 检索接入（`app/application/rag_service.py`）

```text
_resolve_branches -> _locate：调用 EntityLocator
retrieval_request = 残查询（剥掉已解析的实体名），避免实体名主导 BM25
ES 过滤：entity_ids terms（取代 branch_keys 前缀）
tree_mode 语义：off / shadow / tree（原 boost 取消）
  off    : 不定位，纯传统检索
  shadow : 照常定位并记录诊断，结果仍走传统路径
  tree   : 命中实体则以实体范围为主，全局通道保留为低权重兜底
Agent 规划期已定位时，请求带 entity_ids 即跳过 L0-L4
diagnostics 新增 entity_scope 与 locate（含逐 mention 的层轨迹）
```

`SearchRequest` 新增 `entity_composition` / `min_mount_confidence` / `locate_allow_llm`，
与接口草案 9.2 对齐。

### 2.5 仓储与配置

```text
Postgres：locate_entities_exact / fuzzy / semantic、list_entities_pending_embedding、
          update_entity_embedding；向量以 pgvector 字面量传入，无需额外适配器
InMemory：同套接口，供单测使用
config  ：tree_branch_weight/tree_max_branches/tree_max_branch_keys_per_chunk
          -> tree_mount_weight/tree_max_entities/tree_max_mounts_per_chunk
          tree_mode 校验改为 off / shadow / tree
```

## 3. 验收证据

### 3.1 单元测试

```text
pytest: 98 passed, 4 skipped

新增 test_entity_locator.py（11 例）覆盖：
  - 精确命中并剥离实体名（残查询改写）
  - 多 mention 各自独立解析（不因首个命中而短路）
  - 同名跨 domain 返回全部
  - 长名优先于短名（"青云飞鹏项目" 不被 "青云" 抢占）
  - 包含匹配未经验证必须拒绝
  - 包含匹配经验证后接受
  - 指代 mention 走 L4 而不是猜
  - 无 mention 时保留原查询
  - 查询本身就是实体名时残查询回退为原句
  - 或/与 范围合成
  - 词面层未命中时由语义层兜底

新增 test_entity_embedding_service.py（4 例）与 tree 通道选择用例（6 组断言）
```

### 3.2 真实 ES 过滤

```text
无 entity 过滤      -> 7 条
带不存在的 entity_id -> 0 条
结论：entity_ids terms 过滤在真实索引上生效
```

### 3.3 开发过程中修正的三个缺陷

```text
1. 一个 mention 只产出一个候选，导致同名跨 domain 与多实体查询丢结果
   -> 改为按 mention 返回候选集合，精确匹配保留全部
2. 类型加权（score×1.05）把包含匹配从 0.70 抬到 0.735，绕过了"必须验证"的规则
   -> 包含匹配独立为 substring 方法，规则按方法判定而非按分数
3. L2 的包含匹配会短路 L3，导致 "aims" 无法走到语义层
   -> 置信度不足时继续执行 L3 并合并候选
```

### 3.4 审核闭环端到端验证（真实数据）

在真实组织 scope 造一条候选，走 HTTP 审核接口通过，再查树：

```text
1) 列表   total=1  candidate=青云飞鹏官网项目
2) 详情   status=new mentions=1 score=0.82
3) promote -> status=promoted entity=7dee93a1-... registry_version=1
              branch_refresh_job=ed6237a0-...
4) 树     nodes=2
            domain | project | statistics={"entity_count":1}
            entity | project | 青云飞鹏官网项目 | statistics={"chunk_count":0}
```

验证后已清理测试数据（实体、候选、挂载、刷新任务全部归零）。

### 3.5 前端验收

```text
vue-tsc --noEmit    通过
npm test            72 passed, 0 failed
npm run build       构建成功（45.5s）
GET  /admin/entities?scope_type=organization           -> OK
GET  /admin/entity-candidates/{id}                     -> 200
POST /admin/entity-candidates/{id}/review (promote)    -> promoted
```

### 2.6 前端审核界面

| 文件 | 内容 |
|---|---|
| `apps/web/src/api/rag.ts` | 新增候选列表/详情/审核/实体列表四个接口，管理员请求同时带 `X-User-ID` 与 `X-Organization-Id`，403 文案明确为"没有实体审核权限" |
| `apps/web/src/views/info/InfoEntityReviewPage.vue` | 最小审核页：筛选（类型/状态/名称）、候选表格与多选、批量通过/忽略、详情抽屉（置信度/提及/来源 chunk/证据上下文）、规范名称与类型编辑、合并目标选择、备注、四个动作 |
| `apps/web/src/router.ts`、`InfoOrganizationPage.vue`、`InfoShell.vue` | 路由 `/organization/entity-review`、组织页入口、导航分组与标题 |

页面刻意保持最小：不做树可视化、不做图谱式合并选择器、不引入"试用节点"
（接口 07 无此状态）。旧 mock `InfoKnowledgeStructurePage.vue` 保留为设计参考，
已加"设计稿演示"提示条并从组织页摘掉入口，只保留 URL 直达。

## 4. 未完成项

### 4.2 挂载置信度阈值尚未接入检索

`min_mount_confidence` 已经贯通请求契约，但 ES 侧目前只按 `entity_ids` terms
过滤，未叠加 `entity_mounts.confidence` 的 nested 条件。原因：当前所有挂载都是
`explicit`（confidence=1.0），阈值要到 Phase 2 的 `window_batch` 挂载出现后
才有区分意义。

### 4.3 性能指标待实测

实施计划 3.4 的 p95（常规路径 ≤80ms）、L4 调用率（≤20%）需要真实查询集才能
测量，当前仅有单元测试与单点联调。

## 5. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-10-09 | Phase 1 后端完成：五层定位管线、实体向量回填、检索接入与 tree_mode 切换 |
