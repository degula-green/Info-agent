# 实体节点生成方案 - MVP策略说明

## 文档状态

- **版本**: v2.2
- **状态**: 策略说明（数据模型与 API 不再以本文为准）
- **更新日期**: 2026-10-09
- **定位**: 说明 MVP 阶段的实体节点生成策略、窗口扫描与 LLM 提取 prompt
- **权威来源**: interfaces/07-rag-admin-entity-tree-api.md、树形RAG实施计划.md、实体定位五层管线接口草案.md
- **v1 存档**: 实体节点生成方案-MVP-v1.md

## 0. 本文件定位与已取代内容

v1 是一份包含完整 DDL、API 和前端代码的实施方案，其中数据模型与 API 部分与仓库现有实现冲突。v2 将这些部分移除，只保留仍然有效的策略、prompt 与参考信息。

| 章节 | v2 状态 | 说明 |
|---|---|---|
| 1. MVP 核心原则 | 有效 | 流程与定位仍成立 |
| 2. 数据模型设计 | **已取代** | 见第 2 节映射说明，权威为现有表 |
| 3.1 LLM 提取 prompt | 有效 | 已按 5 类 domain 与关系输出修正 |
| 3.1 save_as_pending / _mount_chunks / worker | **已取代** | 见第 3.2、3.3 节重写版 |
| 3.2 人工审核 API | **已取代** | 权威为接口 07 |
| 3.3 实体管理 API | **已取代** | 权威为接口 07 |
| 4. 未来自动化扩展点 | 有效 | 归入 Phase 4 |
| 5. 前端界面 | 参考 | 组件可复用，API 调用需重新对接 |
| 6. 成本估算 | 已修正 | 见第 6 节 |
| 7. 实施计划 | **已取代** | 见 树形RAG实施计划.md |
| 8/9. 风险与指标 | 参考 | 见新实施计划第 7、8 节 |

## 1. MVP 核心原则

### 1.1 设计理念

```yaml
MVP目标:
  - 快速上线，人工审核兜底
  - 保留自动化扩展空间，但不在本期启用

非目标（MVP不做）:
  - 自动消歧
  - 自动合并
  - 复杂规则引擎
  - 实体推荐
```

### 1.2 核心流程

```text
消息到达 → 传统RAG（立即可用）
         ↓
定时任务（窗口扫描）
         ↓
LLM 批量提取实体与关系
         ↓
  ┌──────┴──────┐
  │             │
已知实体      未知实体
  │             │
直接挂载      写入 entity_candidates
  │             │
  │          人工审核
  │        promote / merge / ignore / defer
  │             │
  └──────┬──────┘
         ↓
active 实体参与树形检索
         ↓
【Phase 4 预留】自动合并建议
```

关键分工（避免自动化与审核打架）：

```text
已知实体  → 自动挂载（带置信度，mount_method = window_batch）
未知实体  → 进候选审核，审核通过后再挂载历史 mention
```

## 2. 与现有表的映射

v1 建议新建 `entities`、`chunk_mounts`、`entity_merge_history`、`entity_relation_candidates` 等表。现有实现已经覆盖其中大部分能力，且接口 07 已冻结。**本期在这些表上原地改造，不新建重名表。**

### 2.1 映射总表

| v1 设计 | 现有实现 | v2 处理 |
|---|---|---|
| `entities`（含 `owner_user_id` / `aliases TEXT[]` / `extraction_info` / `status=pending`） | `entity_registry` + `entity_aliases` | 原地改造 `entity_registry`（加向量与统计字段、收窄 5 类 domain）；别名继续用独立表，不用 `TEXT[]` |
| `chunk_mounts`（新建） | `chunk_branches` | 原地改造：加 `confidence` / `mount_method`，删 `branch_key` 与 `tree_nodes` 外键 |
| 待审核实体（`status=pending`） | `entity_candidates` + `entity_candidate_mentions` | 沿用候选表，不把"待审核"放到正式实体上 |
| 审核操作与幂等 | `entity_review_requests` | 沿用，动作 promote / merge / ignore / defer |
| `entity_merge_history` | 无 | **Phase 4 预留**，本期不建表 |
| `entity_relation_candidates` | 无 | **本期不建**，高置信关系直接写入 `entity_relations` |
| `entity_relations` | 无 | 本期新建（见新实施计划 Phase 0） |
| `processed_for_entities`（布尔标记） | `projection_records` 状态机 | **不引入**，改用处理水位线 |
| `merge_suggestions` JSONB | 无 | **Phase 4 预留**，本期不加字段 |

### 2.2 明确不引入的设计

```text
- 不用 owner_user_id / organization_id 双字段表达归属，统一用 scope_type + scope_id
- 不用 aliases TEXT[]，用 entity_aliases 表（支持别名状态与来源）
- 不用 entities.status='pending' 表达待审核，用 entity_candidates.status
- 不用 extraction_info 存 LLM 原始输出，证据存 entity_candidate_mentions
- 不用 processed_for_entities 布尔位，用水位线（见 3.2）
```

### 2.3 状态机说明

候选与正式实体是**两套状态机**，不要混用：

```yaml
候选状态（entity_candidates.status）:
  new / grouped / review_ready / merged / promoted / ignored / deferred

正式实体状态（entity_registry.status）:
  active / disabled / merged

约束:
  - 候选不属于正式树，不参与检索
  - promote 才创建正式实体并通过 registry_version 递增触发缓存失效
  - merge 只添加别名并保留目标实体 ID
```

## 3. 窗口扫描与提取

### 3.1 LLM 提取 prompt

这是 v1 中最有价值的部分，本节保留并做两处修正：domain 收敛为 5 类、输出增加关系字段。

```text
## 任务
从对话中提取实体（公司、人名、项目、政策、合同），以及实体之间的关系。

## 已知实体（如果对话提到，告诉我是哪个已知实体）
{existing_text}

## 对话内容
{window_text}

## 提取规则
1. 提取所有被实际讨论的具体实体（不要泛指）
2. 包括简称、昵称（如"aims"、"小张"）
3. 如果是已知实体的简称，返回已知实体的ID
4. 如果是新实体，给出你认为的规范名称
5. 多次提到的实体更重要
6. 类型必须从 organization|person|project|policy|contract 中选一个；
  无法判断类型时，丢弃该候选，不要猜测
7. 关系类型必须从下面 8 个枚举值中选一个，不要自创：
   works_for | participates_in | belongs_to | governed_by |
   signed_by | related_to | applies_to | contacts
   （discusses / mentions / talks_about 等一律不允许输出）
8. 只在关系稳定且明确时才输出，不要把"同一次对话里提到"当成关系
9. 不要把凭据类信息当作实体：账号、密码、token、密钥、验证码、
   身份证号、银行卡号等一律不提取，也不要写进 evidence

## 输出格式
返回JSON对象：
{
  "entities": [
    {
      "existing_entity_id": "如果匹配到已知实体，填写ID；否则null",
      "name": "规范化名称（或原始提法）",
      "type": "organization|person|project|policy|contract",
      "confidence": 0.0-1.0,
      "mentions": ["对话中的各种叫法"],
      "evidence": "为什么认为这是实体的依据"
    }
  ],
  "relations": [
    {
      "source": "实体名称或已存在的ID",
      "target": "实体名称或已存在的ID",
      "type": "participates_in",
      "confidence": 0.0-1.0
    }
  ]
}

如果没有任何实体或关系，返回 {"entities": [], "relations": []}。
```

调用要点：

```yaml
实测基线（2026-10-09，deepseek-flash）:
  默认档:                  10.3s / completion 1823 / reasoning 1741
  reasoning_effort=low:     5.6s / completion 1125 / reasoning 1087
  reasoning_effort=minimal: 2.7s / completion  440 / reasoning  402
  thinking=disabled:        1.3s / completion   66 / reasoning    0   ← 默认采用

调用参数:
  温度: 0（确定性输出）
  思考: thinking = {"type": "disabled"}
        质量不达标时回退到 reasoning_effort = "minimal"，不使用默认档
  max_tokens: >= 2000
  response_format: {"type": "json_object"}
  API key: 从 services/rag/.env 读取，不要写进代码或本文档

输出校验:
  - content 为空 → 视为调用失败并重试，不能当成"窗口无实体"
    （实测 max_tokens 过小时推理会吃满预算，正文静默返回空串）
  - type 不在 5 类之内 → 丢弃该实体
  - relation.type 不在 8 个枚举内 → 丢弃该关系
  - 关系两端必须能在本次返回的 entities 或已知实体中解析到
  - 实体名称或 evidence 命中凭据模式（密码 / token / 密钥 / 验证码 / 长数字串）
    → 丢弃该实体；写入 entity_candidates.sample_context 前必须脱敏

缓存:
  - key = 窗口内容 hash
  - TTL 1 小时，避免重复扫描重复计费

已有实体参考:
  - 只传当前 scope 的 active 实体摘要（名称 + 别名 + 类型）
  - 数量超限时按最近活跃度截断，避免 prompt 过长
```

真实窗口实测（2026-10-09，6 个 20 条消息窗口，取自库内真实群聊 / 私聊）：

| 配置 | 延迟 | prompt | completion | JSON | 实体数 | 关系数 |
|---|---|---|---|---|---|---|
| `thinking=disabled` | 2.2 ~ 4.0s | 434 ~ 1104 | 134 ~ 364 | 6/6 通过 | 3 / 4 / 4 / 4 / 3 / 5 | 3 / 2 / 3 / 1 / 0 / 4 |
| `reasoning_effort=minimal` | 3.8 ~ 26.1s | 475 ~ 1128 | 287 ~ 4000 | 5/6 通过（一次截断） | 2 / 0 / 2 / 3 / 0 / 5 | 0 / 0 / 0 / 0 / 0 / 1 |

结论：

```text
- 关闭推理不只是更快，质量也更好：实体与关系都更多，且没有一次被 max_tokens 截断
- minimal 在长窗口上会把 4000 token 预算全部用于推理并导致 JSON 截断，
  因此不建议作为质量回退档；若关闭推理确实不达标，应改用更大的 max_tokens
  配合 low 档，而不是 minimal
- 真实数据暴露一个质量问题：窗口里"数据库账号root 密码123456"被当成实体
  （type=project, conf=0.5）。凭据类信息必须靠规则 9 与落库前脱敏双重拦截，
  否则会进入 entity_candidates 甚至正式实体树
- 5 类 domain 的一个副作用：服务器、设备这类对象没有归属类型，
  实测中"116服务器"被归为 project，属可接受的临时降级
```

### 3.2 窗口参数与扫描水位线

```yaml
窗口参数:
  size: 20 条消息
  step: 10 条（50% 重叠）
  分组: 按会话（conversation）分组后再切窗

参数取舍:
  - 太小（5 条）：上下文不足，识别不准
  - 太大（50 条）：容易跨越多个话题
  - 50% 重叠：避免边界消息被漏掉
```

**处理进度用水位线，不用布尔标记。** v1 建议给 chunks 加 `processed_for_entities` 布尔列，这会与 `projection_records` 的状态机形成双写。改用：

```text
每条会话维护 last_processed_sent_at（或最后一个已处理 chunk 的排序键）
扫描时只取 sent_at 晚于水位的消息
处理成功后推进水位；失败不推进，天然支持重试与断点续跑
```

扫描还需要分批与限流，避免一次全量扫描压垮数据库和 LLM 配额：

```yaml
单次批量: 每个 scope 每次最多 N 个会话窗口（可配置）
并发: 控制 LLM 并发，避免触发限流
退避: LLM 失败按指数退避重试，超过阈值标记待人工处理
```

### 3.3 挂载规则

v1 的 `_mount_chunks` 用 `mention.lower() in message.content.lower()` 做子串匹配，会过度挂载（短名命中长名、子串碰撞）。正确做法是复用现有 `EntityMatcher`：**归一化后最长优先、互不重叠**。

修正后的挂载流程：

```python
def mount_window(window, entities, aliases, registry_version):
    """把窗口内每条消息挂载到匹配到的实体。"""
    matcher = EntityMatcher(entities, aliases, registry_version=registry_version)
    for message in window:
        # 1) 用与索引侧一致的三段文本做匹配，避免只匹配正文导致漏挂
        haystack = "\n".join(
            value for value in (
                getattr(message, "title", None),
                getattr(message, "file_name", None),
                " / ".join(getattr(message, "heading_path", ()) or ()),
                message.content,
            ) if value
        )
        # 2) 最长优先、互不重叠，返回 match_method / match_score
        matches = matcher.match_text(haystack)
        for match in matches:
            repository.upsert_mount(
                chunk_id=message.chunk_id,
                entity_id=match.entity_id,
                confidence=MOUNT_CONFIDENCE["explicit"],
                mount_method="explicit",
            )
    # 3) LLM 认为窗口主题相关但未词面命中的消息，
    #    按 confidence 阈值挂载为 window_batch（见 3.4）
```

与 v1 的三点差异：

```text
1. 匹配范围包含 title / file_name / heading_path / content，与索引侧保持一致
2. 匹配算法统一走 EntityMatcher，避免两套逻辑产生不一致的挂载
3. 未知实体不直接挂载，写入 entity_candidates 等审核
```

### 3.4 挂载置信度分级

```yaml
explicit:      0.95  # 消息明确提到实体（EntityMatcher 命中）
window_strong: 0.85  # 窗口主题明确，消息相关性强
window_weak:   0.65  # 窗口主题明确，消息相关性弱
llm_infer:     0.50  # LLM 推断的隐含关系
```

挂载阈值：`confidence >= 0.65` 才写入，遵循"宁可漏，不要错"。

检索侧按场景取阈值（由 `min_mount_confidence` 参数下发）：

```yaml
严格场景（表单填写、联系方式、政策条款）: >= 0.85
探索场景（了解项目情况、泛化问答）:        >= 0.65
```

## 4. 人工审核链路

v1 在第 3.2、3.3 节自定义了一套 `/api/entities/*` 接口（pending / approve / reject / batch-approve / merge / search）。该套接口与已冻结的接口 07 冲突，本节以接口 07 为准。

### 4.1 权威接口

```text
GET    /api/v1/admin/entity-tree                      查看组织树概览
GET    /api/v1/admin/entity-tree/nodes/{node_id}       节点详情
GET    /api/v1/admin/entity-candidates                候选列表
GET    /api/v1/admin/entity-candidates/{id}            候选详情（含脱敏证据）
POST   /api/v1/admin/entity-candidates/{id}/review     审核
GET    /api/v1/admin/branch-refresh-jobs/{job_id}      刷新任务
```

### 4.2 审核动作与结果

```yaml
请求:
  review_request_id: 必填（或使用 Idempotency-Key，两者须一致）
  action: promote | merge | ignore | defer
  target_entity_id: merge 时必填
  canonical_name / domain / note: 可选
  expected_status: 避免并发覆盖

响应:
  candidate_id
  status: promoted | merged | ignored | deferred
  resolved_entity_id
  registry_version
  branch_refresh_job_id
```

规则：

```text
- merge 必须提供 target_entity_id
- promote 不能复用现有正式实体的 normalized key
- 同一候选的同一 review_request_id 只能执行一次（幂等）
- 审核成功后 registry_version 递增，实体定位缓存自然失效
- promote / merge 后创建 branch_refresh_jobs，增量刷新挂载
```

### 4.3 明确不实现

```text
- 不实现 /api/entities/pending、/approve、/reject、/batch-approve、/merge、/search
- 不把"待审核"状态放到正式实体上
- 不为审核单独建一套实体表
```

## 5. 未来自动化扩展点（Phase 4）

以下能力依赖 Phase 2/3 积累的审核记录与评估数据，**不提前排期**，也不进入本期迁移。

### 5.1 自动合并建议

```yaml
功能: 识别可能重复的实体，生成建议，由人工确认
前置: 实体量足够，且已有足量人工合并记录作为监督信号
实现要点:
  - 候选发现：名称相似度 + 共现关系 + 同姓氏
  - LLM 判断是否应合并，输出理由与证据
  - 建议需要落库，但本期不建 entity_merge_history，
    可在 Phase 4 单独迁移
指标: 建议准确率 > 80%，人工采纳率 > 60%
```

### 5.2 智能消歧

```yaml
功能: 处理"小张"这类不确定指代，持续观察并关联到真实实体
实现要点:
  - 需要新增不确定状态，会修改 entity_registry.status 约束
  - 必须与 domain 收窄迁移解耦，使用独立的迁移文件
指标: 消歧准确率 > 75%，未确定实体比例 < 15%
```

### 5.3 部分自动批准

```yaml
功能: 学习人工审核模式，对高置信度候选自动批准
实现要点:
  - 先只做"建议"，积累足够样本后再开启自动执行
  - 自动批准必须留可追溯记录，便于回溯与回滚
指标: 自动批准准确率 > 95%，人工工作量下降 50%
```

### 5.4 关系审核候选（本期不做）

v1 设计了 `entity_relation_candidates` 表用于关系审核。本期决定**不建该表**：高置信度关系（>= 0.75）直接写入 `entity_relations`，低置信度丢弃。等出现真实噪声再补审核流程。

## 6. 成本估算（已修正）

v1 的成本估算与它自己推荐的窗口参数不一致：文档写"每小时 1000 条消息 → 40 个窗口"，但按 size=20 / step=10 计算，单个消息流产生的窗口数是：

```text
窗口数 = floor((消息数 - size) / step) + 1
       = floor((1000 - 20) / 10) + 1
       = 99
```

所以 v1 低估了约 2.5 倍。修正后（假设每小时 1000 条消息、单次调用约 2500 tokens、单价 $0.15/1M）：

| 参数 | 窗口数/小时 | 月调用量 | 月成本 |
|---|---|---|---|
| step=10（50% 重叠，v1 推荐） | 99 | 71,280 | ≈ $26.7 |
| step=20（无重叠，可选的降本方案） | 50 | 36,000 | ≈ $13.5 |

补充说明：

```text
- 实际窗口数取决于消息在各会话中的分布，多会话分摊时窗口数会低于上表线性估算
- 若单会话消息少于 size，仍会产生 1 个窗口
- 成本与召回是权衡：step=10 覆盖更全但更贵，step=20 更省但边界消息可能漏挂
- 建议先按 step=10 上线并监控成本，超过预算再降到 step=20
```

其他成本（v1 数据，量级仍可参考）：

```yaml
人工审核:
  - 初期约 30 分钟/天
  - 随实体库增长，审核量下降

数据库存储:
  - 实体与挂载数据量级为 GB 级，可忽略
```

## 7. 审核界面参考

v1 提供了完整的 Vue 页面与卡片代码，组件结构可以复用，但其中调用的接口已不适用，需要重新对接接口 07。

```text
可复用:
  - 待审核列表 + 批量选择
  - 候选卡片：规范名称编辑、别名标签、提取依据、置信度、上下文样例
  - 类型筛选 Tab（organization / person / project / policy / contract）

需要改造:
  - 数据来源改为 GET /api/v1/admin/entity-candidates
  - 详情数据改为 GET /api/v1/admin/entity-candidates/{id}
  - 操作改为 POST /api/v1/admin/entity-candidates/{id}/review
  - 动作由 approve / reject 改为 promote / merge / ignore / defer
  - 提交需带 review_request_id（或 Idempotency-Key）
  - 合并需要选择 target_entity_id，而不是简单确认
```

交互原则（沿用 v1）：

```text
- 候选按提及次数与评分排序，高频优先
- 支持批量 promote，但 merge 必须逐个确认目标实体
- 拒绝需要填写理由，用于后续质量分析
- 证据只展示 display 或脱敏上下文
```

## 8. 风险与对策

```yaml
技术风险:
  LLM提取不准确:
    影响: 高
    对策: 人工审核兜底 + prompt 迭代 + 类型不在 5 类内直接丢弃

  LLM成本超预算:
    影响: 中
    对策: 窗口结果缓存 + 批量处理 + 必要时 step 从 10 调到 20

  挂载噪音:
    影响: 中
    对策: confidence >= 0.65 才挂载 + 严格场景用 >= 0.85 + 人工抽检

产品风险:
  人工审核负担重:
    影响: 高
    对策: 批量操作 + 高频实体优先 + Phase 4 自动化

  实体重复严重:
    影响: 中
    对策: normalized_key 唯一约束 + 合并功能 + 相似度检测

  冷启动检索效果差:
    影响: 低
    对策: 传统 RAG 降级 + 种子实体
```

## 9. 成功指标

```yaml
功能完整性:
  ✅ LLM 能在窗口内提取实体与关系
  ✅ 未知实体进入候选，已知实体自动挂载
  ✅ 人工能审核、修改、合并
  ✅ 树形检索只使用 active 实体

质量指标:
  - LLM 提取准确率 > 70%（MVP 基线，需标注集支撑）
  - 人工批准率 > 80%
  - 挂载准确率 > 85%
  - 平均审核时间 < 30 秒/实体

性能指标:
  - 窗口扫描不影响在线检索延迟
  - 每小时可处理 > 1000 条消息

成本指标:
  - 月 LLM 成本在预算内（按第 6 节修正值评估）
```

指标口径说明：准确率必须基于标注集评估，且区分"实体识别"与"类型判定"两个维度，避免用一个笼统数字掩盖问题。分层延迟与 L4 调用率见 树形RAG实施计划.md 第 7 节。

## 10. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2024-01-XX | 初版，含完整 DDL / API / 前端实现（已存档为 `实体节点生成方案-MVP-v1.md`） |
| v2.0 | 2026-10-09 | 降级为策略说明：移除与现有实现冲突的数据模型与 API，改为表映射说明；保留并修正 LLM prompt；挂载规则改用 EntityMatcher；处理进度改用水位线；修正成本估算；自动化扩展归入 Phase 4 |
| v2.1 | 2026-10-09 | prompt 补 8 类关系枚举硬规则与「不把提及当关系」约束；调用要点补实测基线（thinking=disabled、max_tokens>=2000、空正文重试、并发 8~16）与 key 走 .env 的说明 |
| v2.2 | 2026-10-09 | prompt 增补凭据类信息排除规则（账号/密码/token/密钥/验证码）；补真实 20 条窗口实测与结论（关闭推理质量更优、minimal 会截断、凭据误提取案例） |
