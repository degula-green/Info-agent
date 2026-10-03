# Agent 记忆机制与压缩方案

## 文档状态

- 版本：v1.6
- 状态：Phase 1/2 已实现待评审，Phase 3+ 未开工
- 作者：架构师（Claude）
- 创建日期：2026-10-03
- 目标：为 Agent 服务设计完整的记忆机制和记忆压缩方案
- 范围：Agent 服务、数据库 schema、与 RAG 服务协同

## 1. 背景与现状分析

### 1.1 现有系统状况

当前 Agent 服务已经实现了基础的对话历史功能（`Agent历史问答功能方案.md`
Phase 1 & Phase 2 已完成）：

**已有基础设施**：
- `agent.conversations` 表：会话窗口，包含 `summary` 和 `summary_cursor` 字段（预留）
- `agent.messages` 表：用户可见消息（user/assistant/system）
- `agent.agent_tasks` 表：执行单位，已关联 `conversation_id`
- Conversation API：支持创建、列表、获取、更新、删除会话
- 与 RAG 服务协同：知识检索通过 `services/rag/` 提供

**现有局限**：
1. **无上下文注入**：Agent 虽然保存了消息历史，但 Understanding / Planner / Answer
   当前都没有读取同一 Conversation 的历史，指代追问无法承接
2. **无语义压缩**：长对话没有摘要和预算裁剪，后续一旦注入完整历史会快速推高 token
3. **无长期记忆**：用户偏好、项目事实、重要决策等信息无法跨会话保留
4. **检索效率低**：没有针对记忆的语义检索机制
5. **无记忆管理**：没有记忆的优先级、置信度、过期策略

### 1.2 业界主流方案对比

| 方案 | 短期记忆 | 长期记忆 | 压缩策略 | 检索机制 | 优点 | 缺点 |
|------|---------|---------|---------|---------|------|------|
| **LangChain** | BufferMemory | VectorStore | Sliding Window / Summary | 向量检索 | 生态成熟、易集成 | 过于通用、缺乏分层 |
| **MemGPT** | Working Context | Recall/Archival | 分层管理 | SQL + 向量 | 分层清晰、可控性强 | 实现复杂、需要精细调优 |
| **OpenAI Assistants** | Thread Messages | Assistant Files | 自动压缩 | 黑盒 | 开箱即用 | 不可控、依赖外部服务 |
| **Claude Context** | Full History | 无 | Prompt Caching | 无 | 原生支持 | 成本高、无跨会话记忆 |
| **AutoGPT** | 短期缓冲 | JSON 文件 | 定期总结 | 文件搜索 | 简单直接 | 扩展性差、无语义检索 |

### 1.3 设计目标

1. **渐进式实施**：不破坏现有对话历史功能，分阶段引入记忆能力
2. **成本可控**：在 token 消耗和记忆精度之间找到平衡点
3. **用户可见**：记忆状态对用户透明，支持查看和管理
4. **隐私优先**：记忆严格按 owner 隔离，支持删除和导出
5. **RAG 协同**：记忆检索与知识检索协同工作，不重复建设

## 2. 核心设计理念

### 2.1 记忆分层架构

借鉴 MemGPT 和人类记忆模型，采用**三层记忆架构**：

```text
┌─────────────────────────────────────────────────────────┐
│                     工作记忆 (Working Memory)              │
│              当前对话的最近 N 轮，直接注入 Prompt          │
│                      token 限制：~4K                      │
└─────────────────────────────────────────────────────────┘
                            ▲
                            │ 动态加载
                            ▼
┌─────────────────────────────────────────────────────────┐
│                  会话摘要 (Session Summary)                │
│          当前会话的压缩摘要，定期更新，始终注入             │
│                      token 限制：~1K                      │
└─────────────────────────────────────────────────────────┘
                            ▲
                            │ 语义检索
                            ▼
┌─────────────────────────────────────────────────────────┐
│                   长期记忆 (Long-term Memory)             │
│     跨会话的用户偏好、项目事实、重要决策，按需检索         │
│                    存储：向量 + 结构化                     │
└─────────────────────────────────────────────────────────┘
```

### 2.2 记忆类型定义

| 记忆类型 | 描述 | 示例 | 生命周期 | 检索方式 |
|---------|------|------|---------|---------|
| **事实记忆** | 稳定的项目信息、组织知识 | "青云官网部署在阿里云" | 长期 | 语义检索 |
| **偏好记忆** | 用户的工作习惯、偏好设置 | "用户喜欢简洁的回答" | 长期 | 全局加载 |
| **决策记忆** | 重要的技术决策和理由 | "选择 FastAPI 因为..." | 长期 | 语义检索 |
| **关系记忆** | 人物关系、负责人信息 | "李明负责后端开发" | 长期 | 图谱检索 |
| **临时上下文** | 当前会话的临时信息 | "刚才提到的那个文件" | 会话级 | 滑动窗口 |

### 2.3 与现有系统的关系

```text
┌──────────────┐
│ Conversation │ 一个会话窗口
└──────┬───────┘
       │ 1:N
       ▼
┌──────────────┐
│   Message    │ 用户可见消息
└──────┬───────┘
       │ 压缩提炼
       ▼
┌──────────────┐
│    Memory    │ 结构化记忆
└──────┬───────┘
       │ 向量化
       ▼
┌──────────────┐
│  RAG Index   │ 语义检索（复用 RAG 服务）
└──────────────┘
```

### 2.4 当前实施约束

本版本先解决“同一 Conversation 内上下文可承接”，不直接进入跨会话长期记忆。

必须遵守以下约束：

```text
1. Phase 1 不创建 memory_records，不接 RAG 向量索引。
2. Phase 1 只使用现有 messages 和 conversations.summary 字段。
3. 摘要边界使用 message_id + summary_version，不再把整数 summary_cursor 当作边界。
4. 摘要更新由 conversation_summary_jobs 持久化驱动，使用 compare-and-set；
   失败不推进边界，不阻塞当前 Task。
5. ConversationContext 通过显式参数注入 Understanding/Planner；
   Answer 通过 ExecutionContext 注入，不写入 TaskEnvelope.input。
6. conversation 删除时，会话级摘要、摘要任务和 conversation 记忆必须同步失效。
7. 记忆检索 query = 当前问题 + 最近用户消息 + 会话摘要，不能只使用当前问题。
8. Phase 2 只允许 memory_records.scope='conversation'；session/global 暂不启用。
9. Phase 2 的中文检索以 keywords + simple FTS 为必选基础；
   pg_trgm 在确认扩展权限后启用，不存在的 'chinese' 配置不得作为依赖。
10. 向量记忆必须使用独立索引或命名空间，并强制 owner_user_id 过滤；
    该能力在 Phase 4 前保持关闭。
```

## 3. 数据库设计

### 3.1 记忆表 (memory_records，Phase 2+ 预留)

> Phase 1 不创建本表；本节定义 Phase 2 启用会话内结构化记忆时的目标 schema。

```sql
CREATE TABLE agent.memory_records (
    memory_id uuid PRIMARY KEY,
    owner_user_id text NOT NULL,  -- 沿用现有 TEXT 类型
    organization_id text,
    
    -- 记忆分类
    memory_type varchar(32) NOT NULL,  -- fact, decision, relation, context
    scope varchar(32) NOT NULL,         -- Phase 2 仅 conversation
    
    -- 记忆内容
    title varchar(200) NOT NULL,        -- 简短标题
    content text NOT NULL,              -- 记忆正文
    content_hash char(64) NOT NULL,     -- sha256(content)，用于去重
    memory_key varchar(128) NOT NULL,   -- 稳定去重键，不直接暴露原文
    keywords text[] DEFAULT '{}',       -- 关键词数组，用于快速过滤
    
    -- 来源追溯
    source_conversation_id uuid NOT NULL
        REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
    source_message_ids uuid[] DEFAULT '{}',  -- 兼容只读展示；来源关系统一写 memory_sources
    extraction_method varchar(32),      -- manual, auto_summary, llm_extract
    extraction_job_id uuid,             -- Phase 3 的提炼 job；当前不建外键，job 表落地后补 FK
    
    -- 质量与置信度
    confidence numeric(4,3) NOT NULL DEFAULT 0.800,  -- 0.000 ~ 1.000
    importance numeric(4,3) NOT NULL DEFAULT 0.500,  -- 重要性评分
    
    -- 访问统计
    access_count int NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    
    -- 生命周期
    status varchar(32) NOT NULL DEFAULT 'candidate',  -- candidate, active, archived, superseded, deleted
    superseded_by_memory_id uuid REFERENCES agent.memory_records(memory_id) ON DELETE SET NULL,
    expires_at timestamptz,                        -- 可选的过期时间
    deleted_at timestamptz,                        -- 软删除时间；物理删除留给隐私清理任务
    
    -- 向量化（预留，实际向量可能存在 RAG）
    embedding_model varchar(64),
    embedding_version int,
    
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    
    CONSTRAINT memory_type_chk 
        CHECK (memory_type IN ('fact', 'decision', 'relation', 'context')),
    CONSTRAINT memory_scope_chk
        CHECK (scope IN ('conversation')),
    CONSTRAINT memory_status_chk
        CHECK (status IN ('candidate', 'active', 'archived', 'superseded', 'deleted')),
    CONSTRAINT confidence_range_chk 
        CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT importance_range_chk 
        CHECK (importance >= 0 AND importance <= 1)
);

-- 索引
CREATE INDEX memory_owner_active_idx 
    ON agent.memory_records (owner_user_id, status) 
    WHERE status = 'active';

CREATE INDEX memory_conversation_idx 
    ON agent.memory_records (source_conversation_id) 
    WHERE source_conversation_id IS NOT NULL;

CREATE INDEX memory_type_importance_idx 
    ON agent.memory_records (owner_user_id, memory_type, importance DESC) 
    WHERE status = 'active';

CREATE INDEX memory_keywords_gin_idx 
    ON agent.memory_records USING gin (keywords);

-- 同一会话内 active 记忆去重；跨会话 global 记忆使用独立表，避免误级联。
CREATE UNIQUE INDEX memory_active_dedup_idx
    ON agent.memory_records (
        owner_user_id, source_conversation_id, memory_type, memory_key
    )
    WHERE status = 'active';

-- 同一提炼 job 内的候选记忆必须幂等，避免重试产生重复候选。
CREATE UNIQUE INDEX memory_candidate_dedup_idx
    ON agent.memory_records (
        owner_user_id, source_conversation_id, extraction_job_id, memory_key
    )
    WHERE status = 'candidate' AND extraction_job_id IS NOT NULL;

-- 全文搜索索引：Phase 2 基础能力使用 simple；'chinese' 配置不存在，禁止依赖。
CREATE INDEX memory_content_fts_idx 
    ON agent.memory_records USING gin (to_tsvector('simple', content));
```

`pg_trgm` 的处理规则：

```text
1. keywords + simple FTS 是 Phase 2 的必选基础能力。
2. 当前数据库 pg_trgm 仅 available、未安装；部署前先验证 CREATE EXTENSION 权限。
3. 有权限时，在独立 migration 中安装 pg_trgm 并增加 trigram 索引。
4. 无权限时，Phase 2 只使用 keywords + simple FTS + ILIKE，不阻塞上线。
5. 不得把 'chinese' text search configuration 作为硬依赖。
```

来源消息不要依赖 `source_message_ids` 数组做级联。后续新增
`agent.memory_sources(memory_id, message_id)` 关联表，并对 message_id 使用
`ON DELETE CASCADE`；数组字段只保留为查询便利，不作为引用完整性依据。

```sql
CREATE TABLE agent.memory_sources (
    memory_id uuid NOT NULL
        REFERENCES agent.memory_records(memory_id) ON DELETE CASCADE,
    message_id uuid NOT NULL
        REFERENCES agent.messages(message_id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, message_id)
);
```

### 3.2 会话摘要扩展

现有 `agent.conversations` 表已经预留了字段，补充设计：

```sql
-- 现有字段说明
-- summary: text                    -- 会话的压缩摘要
-- summary_cursor: integer          -- Phase 1 遗留字段，不再作为可靠边界

-- 建议新增字段（可选，在后续迁移中加入）
ALTER TABLE agent.conversations 
    ADD COLUMN IF NOT EXISTS summary_updated_at timestamptz,
    ADD COLUMN IF NOT EXISTS summary_method varchar(32) DEFAULT 'incremental',
    ADD COLUMN IF NOT EXISTS summary_token_count int DEFAULT 0,
    ADD COLUMN IF NOT EXISTS summary_until_message_id uuid
        REFERENCES agent.messages(message_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS summary_version bigint NOT NULL DEFAULT 0;

COMMENT ON COLUMN agent.conversations.summary IS '会话压缩摘要，每 N 轮或超过阈值时更新';
COMMENT ON COLUMN agent.conversations.summary_cursor IS '遗留计数字段；Phase 1 停止依赖，后续可删除';
COMMENT ON COLUMN agent.conversations.summary_until_message_id IS '摘要覆盖到的最后一条 message_id，消息按 (created_at, message_id) 排序';
COMMENT ON COLUMN agent.conversations.summary_version IS '摘要乐观锁版本；每次成功更新 +1，用于 compare-and-set';
COMMENT ON COLUMN agent.conversations.summary_method IS 'incremental: 增量更新, full: 完全重写';
COMMENT ON COLUMN agent.conversations.summary_token_count IS '摘要的 token 数量估算';
```

如果 `summary_until_message_id` 因消息删除变成 `NULL`，已有 `summary` 必须视为失效：
上下文组装器应忽略该摘要，并在下一个安全时点重新生成，不能继续使用不确定边界的摘要。

### 3.2.1 摘要任务表（Phase 1 必选）

摘要更新必须持久化，不能在 worker 内存里 best-effort 执行。

```sql
CREATE TABLE agent.conversation_summary_jobs (
    job_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL
        REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
    expected_summary_version bigint NOT NULL,
    boundary_from_message_id uuid,
    boundary_to_message_id uuid NOT NULL
        REFERENCES agent.messages(message_id) ON DELETE CASCADE,
    status varchar(32) NOT NULL DEFAULT 'pending',
    attempt_count int NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner varchar(128),
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT summary_job_status_chk
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    UNIQUE (conversation_id, expected_summary_version, boundary_to_message_id)
);

CREATE INDEX conversation_summary_jobs_claim_idx
    ON agent.conversation_summary_jobs (status, available_at, created_at)
    WHERE status IN ('pending', 'failed');
```

说明：

```text
1. Task 进入终态后，只负责创建/复用 job，不在请求链里同步生成摘要。
2. 现有 Agent worker 轮询该表，使用 FOR UPDATE SKIP LOCKED 领取任务。
3. 摘要成功后用 expected_summary_version 做 CAS，再写 conversations；
   CAS 失败则把 job 标记 succeeded 但本次不覆盖新摘要。
4. 失败按 attempt_count 退避重试；不阻塞 Task 终态。
```

### 3.3 会话摘要版本表（Phase 1 不实施，未来可选）

`conversations` 只保存当前生效摘要；如果需要 `summary-history` API、回滚或摘要质量对比，
应增加版本表，而不是依赖覆盖式更新。

```sql
CREATE TABLE agent.conversation_summaries (
    summary_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL
        REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
    version bigint NOT NULL,
    from_message_id uuid,
    to_message_id uuid NOT NULL,
    content text NOT NULL,
    token_count int NOT NULL DEFAULT 0,
    model varchar(128),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, version)
);

CREATE INDEX conversation_summaries_recent_idx
    ON agent.conversation_summaries (conversation_id, version DESC);
```

Phase 1 明确**不创建该表，也不实现** 7.2 的 `summary-history` API。
未来需要摘要历史、回滚或质量对比时再作为独立迁移加入。

### 3.4 记忆访问日志（可选，用于优化）

```sql
CREATE TABLE agent.memory_access_log (
    log_id uuid PRIMARY KEY,
    memory_id uuid NOT NULL REFERENCES agent.memory_records(memory_id) ON DELETE CASCADE,
    task_id text,  -- 哪个 Task 访问的
    conversation_id uuid,
    access_reason varchar(32),  -- planning, answering, context_building
    relevance_score numeric(4,3),  -- 检索时的相关性得分
    was_useful boolean,  -- 后续可通过反馈标记
    accessed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX memory_access_memory_idx 
    ON agent.memory_access_log (memory_id, accessed_at DESC);
```

`access_count` 和 `last_accessed_at` 不在检索请求内同步更新热点行。
Phase 2 先记录日志，统计值由后台批量汇总，避免每次 Planner/Answer
检索都产生额外行锁写竞争。

## 4. 记忆压缩策略

### 4.1 会话摘要生成

**触发时机**：
1. **轮次阈值**：每 5 个 completed turn 触发一次增量更新
   （1 个 turn = 1 条 completed user message + 1 条 completed assistant message，即 10 条 message）
2. **Token 阈值**：历史消息总 token 数超过 8K
3. **手动触发**：用户或系统管理员手动请求

摘要统计只计算当前 Task 之前、状态为 `completed` 的 user/assistant messages；
pending、failed、cancelled 和当前 Task 自己的消息不参与轮次和 token 计数。

**Phase 1 模型与预算**：

```text
1. 默认复用 AGENT_LLM_* 配置，新增 AGENT_SUMMARY_MAX_OUTPUT_TOKENS=800。
2. 摘要目标长度 500 字，注入预算 1K token 左右。
3. Phase 1 使用近似 token 估算：UTF-8 字节数 / 3，向上取整；后续再引入真实 tokenizer。
4. 摘要任务由 conversation_summary_jobs 驱动，worker 失败不阻塞 Task。
```

**压缩算法**：

```python
# 伪代码
def update_conversation_summary(conversation_id: str) -> str:
    """增量更新会话摘要"""
    conversation = store.get_conversation(conversation_id)
    current_summary = conversation.summary or ""
    expected_version = conversation.summary_version
    boundary_message_id = conversation.summary_until_message_id
    
    # 只读取 boundary 之后的消息；没有 boundary 时从头开始。
    # 排序必须是稳定的 (created_at, message_id)。
    new_messages = store.list_messages_after_boundary(
        conversation_id,
        boundary_message_id,
    )
    if len(new_messages) < 10:  # 不足 5 个 completed turn，不值得更新
        return current_summary
    
    # 构建压缩 prompt
    prompt = f"""
现有摘要：
{current_summary}

新的对话内容：
{format_messages(new_messages)}

请更新摘要，要求：
1. 保留关键信息：问题、结论、决策、待办事项
2. 去除冗余：重复的寒暄、无效的尝试
3. 使用第三人称：用户询问了...，助手回答了...
4. 控制长度：不超过 500 字

更新后的摘要：
"""
    
    new_summary = llm_client.complete(prompt)
    
    # compare-and-set：并发摘要只能有一个成功推进边界。
    updated = store.compare_and_set_summary(
        conversation_id=conversation_id,
        expected_version=expected_version,
        summary=new_summary,
        summary_until_message_id=new_messages[-1].message_id,
        summary_token_count=estimate_tokens(new_summary),
        updated_at=utcnow(),
    )
    if not updated:
        # 其他 Task 已推进摘要；本次不覆盖，也不推进 boundary。
        return store.get_conversation(conversation_id).summary

    return new_summary
```

摘要更新必须满足：

```text
1. 只在 Task 进入终态后触发，不能在 user/assistant message 写入一半时总结。
2. summary 与 summary_until_message_id、summary_version 在同一事务内提交。
3. CAS 失败时不覆盖新摘要，也不回退 boundary。
4. 摘要失败不影响 Task 成功状态；下一轮可重试。
```

**摘要格式示例**：

```text
[会话主题] 青云官网部署问题排查

[进展摘要]
用户询问青云官网的部署状态。助手查询本地知识库，发现官网已部署在阿里云，
使用 Docker Compose 编排，当前运行正常。用户追问了 SSL 证书配置，助手
确认证书由 Let's Encrypt 自动更新。

[关键决策]
- 采用 Nginx 反向代理 + Docker Compose 方案
- SSL 证书自动续期，无需人工干预

[待办事项]
- 下周一检查日志轮转配置
- 月底前完成监控告警接入

[遗留问题]
- 数据库备份策略尚未确认
```

### 4.2 滑动窗口策略

**工作记忆加载逻辑**：

```python
def build_working_memory(
    conversation_id: str,
    current_task_id: str,
    max_tokens: int = 4000,
) -> list[Message]:
    """构建工作记忆：摘要 + 当前 Task 之前的已完成消息。"""
    conversation = store.get_conversation(conversation_id)

    # 只读取 boundary 之后、当前 Task 之前、状态 completed 的消息。
    # summary_until_message_id 为 NULL 且 summary 非空时，摘要视为失效。
    boundary = conversation.summary_until_message_id
    if conversation.summary and boundary is None:
        summary = None
    else:
        summary = conversation.summary
    messages = store.list_completed_messages_after_boundary(
        conversation_id,
        boundary_message_id=boundary,
        exclude_task_id=current_task_id,
        order_by="(created_at, message_id) DESC",
    )
    
    # 1. 始终包含会话摘要（如果有）
    context = []
    token_count = 0
    
    if summary:
        summary_msg = Message(
            role="system",
            content=f"[会话摘要]\n{summary}"
        )
        context.append(summary_msg)
        token_count += estimate_tokens(summary)
    
    # 2. 从最新往前加载消息
    recent_messages = []
    for msg in messages:
        msg_tokens = estimate_tokens(msg.content)
        if token_count + msg_tokens > max_tokens:
            break
        recent_messages.insert(0, msg)  # 保持时间顺序
        token_count += msg_tokens
    
    context.extend(recent_messages)
    return context
```

当前用户问题不在 `build_working_memory` 中重复加入；它只在调用下游时从
`task.input["text"]` 注入一次。Runtime 必须以 `conversation_id + current_task_id`
调用该函数，禁止读取当前 Task 正在写入的 pending assistant message。

### 4.3 候选记忆提炼（Phase 3，未来）

Phase 1/2 不启用本节。Phase 3 只生成当前 Conversation 内的 `candidate` 记忆，
不写 `global`，不自动注入 Planner/Answer。只有用户确认或明确规则通过后才转为 `active`。

**提炼时机**：
- Task 进入终态后，异步分析尚未提炼的新消息
- 用户主动标记（"记住这个"）
- 定期扫描满足质量条件的会话

**提炼规则**：

```python
def extract_candidate_memories(
    conversation_id: str,
    job_id: str,
) -> list[MemoryRecord]:
    """从会话增量提炼候选记忆；不自动激活、不写 global。"""
    conversation = store.get_conversation(conversation_id)
    job = store.get_memory_extraction_job(job_id)
    messages = store.list_messages_after_boundary(
        conversation_id,
        job.high_watermark_message_id,
    )
    if not messages:
        return []
    
    prompt = f"""
分析以下对话，提炼出值得长期保留的记忆。

对话内容：
{format_messages(messages)}

请提炼以下类型的记忆（JSON 格式）：
1. fact: 稳定的事实信息（项目状态、技术栈、部署环境）
2. decision: 技术决策（为什么选择某方案、权衡了什么）
3. relation: 人物关系（谁负责什么、团队结构）
4. context: 当前会话内的稳定上下文（不能是“刚才那个”这类临时指代）

要求：
- 每条记忆必须有明确的标题和内容
- 每条记忆必须给出 `source_message_ids`，且只能来自本次输入的 messages
- 标注置信度（0.0-1.0）和重要性（0.0-1.0）
- 不提炼用户偏好；偏好能力留到跨会话记忆阶段
- 如果没有值得保留的记忆，返回空数组

JSON Schema:
{{
  "memories": [
    {{
      "memory_type": "fact",
      "title": "青云官网部署在阿里云",
      "content": "青云官网使用 Docker Compose 部署在阿里云 ECS 上，域名为 qingyun.example.com，使用 Nginx 反向代理和 Let's Encrypt SSL 证书。",
      "keywords": ["青云官网", "阿里云", "Docker", "部署"],
      "source_message_ids": ["m001", "m002"],
      "confidence": 0.95,
      "importance": 0.8
    }}
  ]
}}

输出：
"""
    
    response = llm_client.complete(prompt)
    memories_data = json.loads(response)
    
    memories = []
    for item in memories_data["memories"]:
        memory = MemoryRecord(
            memory_id=str(uuid4()),
            owner_user_id=conversation.owner_user_id,
            organization_id=conversation.organization_id,
            memory_type=item["memory_type"],
            scope="conversation",
            title=item["title"],
            content=item["content"],
            keywords=item["keywords"],
            source_conversation_id=conversation_id,
            extraction_method="llm_extract",
            confidence=item["confidence"],
            importance=item["importance"],
            status="candidate",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        memories.append(memory)
        store.create_memory(memory)
        store.add_memory_sources(memory.memory_id, item["source_message_ids"])

    store.complete_memory_extraction_job(
        job_id,
        high_watermark_message_id=messages[-1].message_id,
    )
    
    return memories
```

`create_memory` 与 `memory_sources` 必须在同一事务提交；job 的 high watermark
只能在候选记忆全部写成功后推进。重试时依靠 `memory_key/content_hash` 幂等去重。

## 5. 记忆检索机制

### 5.1 混合检索策略

结合多种检索方式，按优先级组合：

```python
def retrieve_relevant_memories(
    owner_user_id: str,
    query_context: str,
    conversation_id: str | None = None,
    top_k: int = 5,
    scope_policy: str = "conversation_only",
) -> list[MemoryRecord]:
    """混合检索相关记忆"""

    # Phase 1 只允许当前 Conversation。跨会话和 global 统一关闭。
    if scope_policy == "conversation_only" and not conversation_id:
        return []
    
    # 1. 全局偏好记忆：Phase 1 固定为空，Phase 4 才允许加载。
    preference_memories = []
    if scope_policy == "cross_session":
        # 未来使用独立 global_memory_records，不允许 conversation scope 表承载 global。
        preference_memories = store.list_global_memories(
            owner_user_id=owner_user_id,
            memory_type="preference",
            status="active"
        )
    
    # 2. 当前会话的记忆（会话级上下文）
    conversation_memories = []
    if conversation_id:
        conversation_memories = store.list_memories(
            owner_user_id=owner_user_id,
            source_conversation_id=conversation_id,
            scope="conversation",
            status="active"
        )
    
    # 3. 关键词匹配（快速过滤）
    keywords = extract_keywords(query_context)
    keyword_memories = store.search_memories_by_keywords(
        owner_user_id=owner_user_id,
        source_conversation_id=conversation_id,
        keywords=keywords,
        limit=top_k * 2
    )
    
    # 4. 向量语义检索：Phase 1 默认关闭。启用时必须走独立 memory index，
    #    并强制 owner_user_id + source_conversation_id 过滤。
    vector_memories = []
    if settings.agent_memory_vector_enabled:
        query_embedding = rag_client.embed_text(query_context)
        vector_memories = rag_client.search_memories(
            owner_user_id=owner_user_id,
            source_conversation_id=conversation_id,
            embedding=query_embedding,
            top_k=top_k * 2,
        )
    
    # 5. 全文搜索（PostgreSQL FTS，补充）
    fts_memories = store.fulltext_search_memories(
        owner_user_id=owner_user_id,
        source_conversation_id=conversation_id,
        query=query_context,
        limit=top_k
    )
    
    # 6. 合并去重，按重要性和相关性排序
    all_memories = merge_and_rank(
        preference_memories,
        conversation_memories,
        keyword_memories,
        vector_memories,
        fts_memories
    )
    
    # 7. 只追加访问日志；access_count/last_accessed_at 由后台批量汇总，
    #    不在检索请求里更新热点行。
    for memory in all_memories[:top_k]:
        store.append_memory_access_log(
            memory_id=memory.memory_id,
            conversation_id=conversation_id,
            access_reason="context_building",
        )
    
    return all_memories[:top_k]
```

### 5.2 记忆排序算法

```python
def calculate_memory_relevance(
    memory: MemoryRecord,
    semantic_score: float,
    current_time: datetime
) -> float:
    """计算记忆的综合相关性得分"""
    
    # 1. 语义相似度 (0.0 - 1.0)，由独立 memory index 返回。
    
    # 2. 重要性 (0.0 - 1.0)
    importance_score = memory.importance
    
    # 3. 置信度 (0.0 - 1.0)
    confidence_score = memory.confidence
    
    # 4. 时间衰减因子
    days_since_created = (current_time - memory.created_at).days
    days_since_accessed = (current_time - memory.last_accessed_at).days if memory.last_accessed_at else days_since_created
    
    # 最近访问的记忆权重更高
    recency_score = math.exp(-0.01 * days_since_accessed)
    
    # 5. 访问频率
    frequency_score = min(1.0, memory.access_count / 10.0)
    
    # 6. 类型权重
    type_weights = {
        "preference": 1.2,   # 偏好记忆优先级最高
        "decision": 1.1,     # 决策记忆次之
        "fact": 1.0,
        "relation": 0.9,
        "context": 0.8       # 临时上下文优先级最低
    }
    type_score = type_weights.get(memory.memory_type, 1.0)
    
    # 综合得分（权重可调）
    final_score = (
        semantic_score * 0.35 +
        importance_score * 0.25 +
        confidence_score * 0.15 +
        recency_score * 0.10 +
        frequency_score * 0.05
    ) * type_score
    
    return final_score
```

## 6. 与 RAG 服务的协同设计

### 6.1 分工边界

| 维度 | RAG 服务 | Agent 记忆 |
|------|---------|-----------|
| **数据源** | 文档、群聊、外部知识 | 对话历史、用户偏好 |
| **生命周期** | 长期稳定，与源文档同步 | 动态演化，可被覆盖 |
| **检索范围** | 全组织、指定知识库 | 当前用户、当前会话 |
| **向量化** | 文档片段（chunk） | 结构化记忆条目 |
| **索引** | 独立的向量数据库 | 复用 RAG 向量基础设施，但使用独立 `agent_memory` 索引/命名空间 |
| **权重** | 权威性高（来自文档） | 个性化高（来自对话） |

### 6.2 协同检索流程

```python
def retrieve_context_for_task(
    task: TaskEnvelope,
    conversation_id: str
) -> dict[str, Any]:
    """为 Task 构建完整上下文"""
    
    query = task.input.get("text", "")
    owner_user_id = task.owner_user_id
    
    # 1. 工作记忆：最近对话
    working_memory = build_working_memory(conversation_id, max_tokens=4000)

    # 记忆检索不能只靠当前一句，否则“继续”“刚才那个”无法命中。
    memory_query = build_memory_query(
        current_question=query,
        recent_messages=working_memory,
        summary=conversation.summary,
    )
    
    # 2. 个人记忆：用户偏好和相关记忆
    personal_memories = retrieve_relevant_memories(
        owner_user_id=owner_user_id,
        query_context=memory_query,
        conversation_id=conversation_id,
        top_k=5,
        scope_policy="conversation_only",
    )
    
    # 3. 知识检索：RAG 服务（如果需要）
    knowledge_chunks = []
    if should_search_knowledge(query):
        knowledge_chunks = rag_client.search(
            query=query,
            owner_user_id=owner_user_id,
            top_k=10
        )
    
    # 4. 组装上下文（Phase 1：工作记忆 > 会话内记忆 > 知识；不加载偏好）
    context = {
        "working_memory": working_memory,
        "relevant_memories": personal_memories,
        "knowledge_chunks": knowledge_chunks
    }
    
    return context
```

### 6.2.1 上下文注入契约

`TaskEnvelope` 不直接携带 conversation history。新增只读
`ConversationContext`，由 Runtime 在单个边界内组装并传给下游，避免
Understanding、Planner、Answer 各自查询数据库。

```python
@dataclass(frozen=True)
class ConversationContext:
    conversation_id: str
    summary: str | None
    summary_until_message_id: str | None
    recent_messages: tuple[MessageRecord, ...]
    relevant_memories: tuple[MemoryRecord, ...]
```

注入范围和预算：

```text
Understanding:
  当前用户文本为主
  + 最近 2 轮消息
  + summary 的紧凑指代段
  不直接注入全部 relevant_memories

Planner:
  当前问题
  + summary
  + 最近消息
  + 当前会话 relevant_memories

Answer:
  当前问题
  + summary
  + 最近消息
  + 当前会话 relevant_memories
  + 本轮 capability 证据
```

裁剪顺序固定为：

```text
当前问题 > 最近消息 > 会话摘要 > 会话内记忆
```

**接口落点（必须按此实施）**：

`TaskEnvelope` 不新增 history 字段。Runtime 组装一次只读 `ConversationContext`，
并按下面方式传给不同组件：

```python
class TaskUnderstandingProvider(Protocol):
    def understand(
        self,
        task: TaskEnvelope,
        *,
        conversation_context: ConversationContext | None = None,
        min_confidence: float | None = None,
    ) -> TaskUnderstanding:
        ...


class Planner(Protocol):
    def create_plan(
        self,
        task: TaskEnvelope,
        capabilities: list[CapabilityDescriptor],
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
        *,
        conversation_context: ConversationContext | None = None,
    ) -> Plan:
        ...

    def decide_after_observation(
        self,
        task: TaskEnvelope,
        current_plan: Plan,
        observations: list[Observation],
        constraints: PlanningConstraints,
        understanding: TaskUnderstanding | None = None,
        *,
        conversation_context: ConversationContext | None = None,
    ) -> PlannerDecision:
        ...
```

Answer 的 context 不进入 `answer.compose` 的 Planner-facing schema，也不放进
Capability arguments。扩展已有 `ExecutionContext`：

```python
@dataclass(frozen=True)
class ExecutionContext:
    ...
    conversation_context: ConversationContext | None = None
```

`CapabilityExecutor` 在调用 capability 前绑定该 context；
`AnswerComposeCapability` 通过 `current_execution_context()` 读取，
再传给 `LlmAnswerProvider.compose(..., conversation_context=...)`。
这样 Planning 输入可审计，同时 Answer 不会把历史伪装成普通 evidence。

### 6.3 向量索引复用

这里描述的是 Phase 4 的候选能力，不是 Phase 1 实施内容。

当前 RAG Elasticsearch 索引围绕 Knowledge Chunk、scope、ACL 和知识库设计，
不能直接把 Agent 记忆文档混入同一索引。启用向量记忆前必须新增独立的
`agent_memory` physical index 或命名空间，并满足：

```text
1. 查询强制带 owner_user_id，不能只依赖调用方传参。
2. Phase 1-3 强制带 source_conversation_id。
3. 禁止复用 knowledge 读索引和公开检索入口。
4. 删除 conversation 时同步删除对应 memory vectors。
5. 向量不可用时降级到关键词 + simple FTS，不阻塞 Task。
```

```python
# RAG 服务扩展接口（新增）
class RAGClient:
    def index_memory(
        self,
        memory_id: str,
        content: str,
        metadata: dict[str, Any]
    ) -> str:
        """将记忆索引到向量数据库"""
        embedding = self.embed_text(content)
        doc_id = f"memory:{memory_id}"
        
        self.vector_store.upsert(
            id=doc_id,
            vector=embedding,
            metadata={
                "type": "agent_memory",
                "owner_user_id": metadata["owner_user_id"],
                "source_conversation_id": metadata["source_conversation_id"],
                "scope": metadata["scope"],
                "memory_type": metadata["memory_type"],
                "status": metadata["status"],
                "importance": metadata["importance"],
                "created_at": metadata["created_at"]
            }
        )
        
        return doc_id
    
    def search_memories(
        self,
        owner_user_id: str,
        source_conversation_id: str,
        embedding: list[float],
        top_k: int = 10
    ) -> list[dict]:
        """从向量数据库检索记忆"""
        results = self.vector_store.search(
            vector=embedding,
            filter={
                "type": "agent_memory",
                "owner_user_id": owner_user_id,
                "source_conversation_id": source_conversation_id,
                "scope": "conversation",
                "status": "active"
            },
            top_k=top_k
        )
        
        return results
```

## 7. API 设计

### 7.1 记忆管理 API

Phase 2 只提供 conversation-scoped 的最小管理能力。所有接口沿用 Core JWT 和
`owner_user_id` 隔离；客户端不得传 owner_user_id。删除 conversation 后，
对应 memory API 不应再返回记录。

```text
# 列出当前会话的记忆
GET /api/agent/v1/conversations/{conversation_id}/memories

# 手动创建当前会话的记忆
POST /api/agent/v1/conversations/{conversation_id}/memories
Body: {
  "memory_type": "fact",
  "title": "项目使用 FastAPI 框架",
  "content": "当前项目后端采用 FastAPI 框架...",
  "keywords": ["FastAPI", "后端", "框架"],
  "importance": 0.8
}

# 删除当前会话的一条记忆
DELETE /api/agent/v1/conversations/{conversation_id}/memories/{memory_id}
```

Phase 2 的服务端约束：

- `conversation_id` 必须属于当前 JWT 用户，否则返回 403
- 客户端传入的 `owner_user_id` 一律忽略
- Phase 2 只接受 `scope='conversation'`；`session/global` 暂不启用
- Phase 2 拒绝 `memory_type='preference'`；偏好属于未来跨会话记忆，不在本表承载
- 手动创建的稳定记忆可以写 `status='active'`，自动提炼只能写 `status='candidate'`

以下接口属于 Phase 3/4，不在当前实现范围：

```text
PATCH  /memories/{memory_id}
GET    /memories/search
GET    /memories/export
DELETE /memories
```

### 7.2 会话摘要 API

```text
# 触发会话摘要更新
POST /api/agent/v1/conversations/{conversation_id}/summarize
Response: {
  "conversation_id": "uuid",
  "summary": "更新后的摘要...",
  "summary_until_message_id": "uuid",
  "summary_version": 3,
  "token_count": 450
}

# 获取会话摘要历史（Phase 1 不实现；仅在 3.3 conversation_summaries 建表后启用）
GET /api/agent/v1/conversations/{conversation_id}/summary-history
```

Phase 1 只实现摘要生成和当前 active summary，不实现此接口。

### 7.3 记忆提炼 API

本节属于 Phase 3，不在当前 Phase 1 范围内。落地前必须补充
`agent.memory_extraction_jobs` 表；当前 schema 中还没有 job_id、status、
attempt_count、last_error、finished_at 的持久化载体。

```text
# 从会话提炼记忆（异步）
POST /api/agent/v1/conversations/{conversation_id}/extract-memories
Response: {
  "job_id": "uuid",
  "status": "processing"
}

# 查询提炼任务状态
GET /api/agent/v1/memory-extraction-jobs/{job_id}
Response: {
  "job_id": "uuid",
  "status": "completed",
  "extracted_count": 3,
  "memories": [...]
}
```

## 8. 实施计划（当前版本）

**本轮实施状态（2026-10-03）**：

- 本轮范围只包含 Phase 1 和 Phase 2，Phase 3-5 不实施。
- Phase 1/2 已落到 `codex/agent-memory-phase1-2` 分支：会话摘要边界、
  `ConversationContext` 注入、conversation-scoped `memory_records`、
  keywords + `simple` FTS + `ILIKE` 检索和最小管理 API 均已实现。
- `AGENT_CONVERSATION_CONTEXT_ENABLED` 默认开启；
  `AGENT_CONVERSATION_MEMORY_ENABLED` 默认关闭，需要显式开启后才会把
  conversation memory 注入上下文或对外联调。
- 本轮没有代码级阻塞。后续阶段启动前需要先完成下面的数据模型和产品决策，
  不能直接在 Phase 2 表上继续叠加：
  - Phase 3 缺少 `memory_extraction_jobs` 持久化状态机，无法可靠实现
    候选记忆提炼、重试、幂等和完成/失败状态。启动前必须先补 job 表，
    并以 `(conversation_id, extraction_job_id, memory_key)` 作为候选幂等键。
  - Phase 4 的跨会话记忆不能复用当前 `memory_records`：当前表要求
    `source_conversation_id NOT NULL` 且删除 conversation 会级联删除记忆，
    这与跨会话记忆的生命周期冲突。启动前必须选择独立的
    `global_memory_records` 表或经过验证的 scope 迁移，并补独立的向量
    索引/命名空间、owner 过滤、导出和删除策略。
  - Phase 5 的批量管理、过期策略和全局记忆页面需要先确定产品保留策略
    与租户/企业权限边界，不能在 Phase 2 提前做一个空的管理入口。
  - `pg_trgm` 权限验证不是 Phase 1/2 的阻塞项。当前检索已使用
    keywords + `simple` FTS + `ILIKE`；只有实测召回不足时才需要申请扩展
    权限并增加 trigram 索引。

**记忆管理 API 与前端呈现**：

- Phase 2 保留最小 API 是必要的：它是 owner 隔离、conversation 归属校验和
  后续前端/运营排障的稳定契约，不代表现在必须建设完整的记忆管理页。
- Phase 2 不新增独立导航或页面。前端在会话设置中增加低优先级的
  “本会话记忆”入口，先展示记忆数量，展开后显示 active 记忆、来源和删除操作；
  手动新增只作为明确的高级操作，不打断主聊天流程。
- Phase 3 的候选记忆确认/拒绝应复用同一入口，用“已启用 / 待确认”分段展示；
  只有 Phase 4 明确启用跨会话记忆后，才增加全局记忆中心和批量管理页面。

### Phase 1：会话内上下文与摘要（当前实施，2 周）

**目标**：让同一 Conversation 内的指代、承接和追问成立，并建立稳定的
summary 边界。本阶段**不创建 `memory_records`**，不接向量索引，不加载
global/preference/跨会话记忆。

**交付内容**：

- [x] migration 增加：
  `summary_until_message_id uuid`、`summary_version bigint`、
  `summary_updated_at timestamptz`、`summary_token_count int`；
  `summary_cursor` 保留但停止依赖
- [x] 新增 `conversation_summary_jobs` 持久化任务表和 worker 领取逻辑
- [x] 实现只读 `ConversationContext` 组装器：
  `owner_user_id + conversation_id -> summary + recent_messages`
- [x] 按 `(created_at, message_id)` 稳定排序，摘要边界使用
  `summary_until_message_id`
- [x] 摘要更新使用 compare-and-set：
  只允许一个并发更新成功推进 `summary_version`
- [x] 摘要生成放在 Task 终态后的可重试链路中，失败不阻塞 Task
- [x] 把上下文注入 Understanding、Planner、Answer；Understanding 只注入
  有限指代上下文
- [x] 按 6.2.1 修改协议签名和 `ExecutionContext`，禁止把历史塞进 `TaskEnvelope.input`
- [x] 实现确定性预算：
  `当前问题 > 最近消息 > 会话摘要`
- [x] 增加开关：
  `AGENT_CONVERSATION_CONTEXT_ENABLED`
- [x] Phase 2 再启用 `AGENT_CONVERSATION_MEMORY_ENABLED`；默认关闭，按环境显式开启
- [x] 增加 conversation 删除后不再读取摘要的测试，以及并发摘要测试

**验收标准**：

- 同一会话内 10 轮以上追问可以正确解析指代和承接关系
- 新 conversation 不读取其他 conversation 的消息或摘要
- 用户 A 无法读取用户 B 的会话摘要
- 摘要生成失败时，当前 Task 仍可使用最近消息正常执行
- 摘要和最近消息不重复注入
- 注入总 token 不超过预算，每轮成本有监控数据
- 不产生跨会话副作用

### Phase 2：会话内记忆存储与检索（2 周）

**目标**：建立 Conversation 内的结构化记忆闭环，不启用跨会话记忆。

本阶段只实现后端存储、检索和最小 API，不新增独立的前端记忆管理页面；
会话页面只负责把 Phase 1 的上下文能力用于当前对话。

**交付内容**：

- [x] 创建 `memory_records` 和 `memory_sources`
- [x] `memory_records` 仅允许 `scope='conversation'`；
  `source_conversation_id` 使用 `NOT NULL + ON DELETE CASCADE`
- [x] 增加 `content_hash` / `memory_key` 去重
- [x] 服务层拒绝 `memory_type='preference'`；偏好不在会话内记忆表中承载
- [x] 实现必选的关键词 + PostgreSQL `simple` FTS 检索
- [ ] 验证 pg_trgm 权限；可用时增加 trigram 索引，不可用时降级 ILIKE
- [x] 不接入任何向量检索路径；未来 Phase 4 实施时再引入显式开关
- [x] 检索 query 使用“当前问题 + 最近消息 + summary”
- [x] 实现最小 API：
  `GET/POST /conversations/{id}/memories` 和
  `DELETE /conversations/{id}/memories/{memory_id}`
- [x] 记忆 API 走 Core JWT + owner 隔离，服务端强制 conversation 归属
- [x] 增加删除联动和跨用户隔离测试

**验收标准**：

- 删除 conversation 后，其会话级 memory 同步删除
- 同一会话不会返回其他会话的记忆
- 向量关闭时，关键词/FTS 降级结果可用

### Phase 3：长期记忆提炼候选（3 周）

**目标**：从会话中提炼候选记忆，但暂不自动注入。

**交付内容**：

- [ ] 新增 `memory_extraction_jobs` 持久化表和状态机
- [ ] Task 终态后异步提炼 `candidate` 记忆
- [ ] 使用 `(conversation_id, extraction_job_id, memory_key)` 约束候选幂等
- [ ] 去重、版本管理、置信度和来源追溯
- [ ] 前端支持确认/拒绝候选记忆
- [ ] 候选记忆不进入 Planner/Answer，直到用户或规则确认

### Phase 4：跨会话记忆检索（未来）

**目标**：在隔离和评估通过后启用跨会话记忆。

**前置条件**：

- [ ] 使用独立 `global_memory_records` 或显式 scope 迁移，不能让
  conversation 删除误删 global 记忆
- [ ] 单独的记忆向量索引/命名空间，强制 owner_user_id 过滤
- [ ] 用户偏好和跨会话事实的审计、导出、删除策略
- [ ] A/B 评估证明回答质量和 token 成本均达标

### Phase 5：记忆管理与优化（2 周）

- [ ] 时间衰减和过期策略
- [ ] 批量导出/导入
- [ ] 前端记忆可视化管理
- [ ] 记忆质量、召回率和成本面板
- [ ] 一键删除所有记忆

## 9. 成本与性能估算

### 9.1 Token 消耗估算

| 场景 | 无压缩注入历史（模拟） | Phase 1/2 会话上下文 + 摘要 | 当前阶段收益 |
|------|-------------------|------------------------|------------|
| 10 轮短对话 | ~3K tokens | ~2K tokens (摘要 + 最近 3 轮) | 约 33% |
| 50 轮长对话 | ~15K tokens | ~5K tokens (摘要 + 最近 5 轮 + 会话内记忆) | 约 67% |
| 跨会话复用 | 0 tokens | 不适用 | 留给 Phase 4，不计入当前验收 |

**估算假设**：

当前系统尚未向 Understanding / Planner / Answer 注入完整历史，因此“当前”
列是目标态如果无压缩而注入历史的成本模拟，不代表线上现状。真实收益需要
Phase 1 上线后按实际 prompt 长度重新测量。

- 每条消息平均 150 tokens
- 摘要压缩率 5:1
- 长期记忆平均 200 tokens/条，注入 3-5 条（仅用于 Phase 4 估算）

**预期效果**：
- **短期**：前 10 轮无明显优势，10 轮后开始节省
- **长期**：对话越长，节省越明显，50 轮以上节省 > 60%
- **跨会话**：属于 Phase 4 的独立收益，本阶段不承诺

### 9.2 存储成本

- **记忆记录**：平均 1KB/条，1000 条记忆 = 1MB，可忽略
- **向量索引**：1536 维 float32 = 6KB/条，1000 条 = 6MB，可接受
- **会话摘要**：平均 500 字/会话，1000 会话 = 500KB，可忽略

**结论**：存储成本极低，主要成本在 LLM 调用。

### 9.3 性能指标

| 操作 | 目标延迟 | 预估 QPS |
|------|---------|----------|
| 创建记忆 | < 100ms | 100 |
| 检索记忆（混合） | < 500ms | 50 |
| 生成摘要 | < 5s (异步) | 10 |
| 提炼记忆 | < 10s (异步) | 5 |

## 10. 风险与挑战

### 10.1 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **LLM 提炼质量不稳定** | 高 | 多次采样 + 人工审核 + 置信度阈值 |
| **向量检索召回率低** | 中 | 混合检索（关键词 + 向量 + FTS） |
| **记忆冲突和矛盾** | 中 | 版本管理（superseded_by）+ 时间戳 |
| **性能瓶颈（向量检索）** | 中 | 缓存 + 索引优化 + 降级策略 |
| **Token 消耗反增** | 高 | 严格的 Token 预算 + 监控告警 |
| **摘要边界漂移或重复注入** | 高 | message_id 边界 + 版本 CAS + 最近消息去重 |
| **私有记忆混入知识索引** | 高 | 独立 index/namespace + 强制 owner_user_id 过滤 |
| **中文 FTS 配置不可用** | 中 | simple/pg_trgm 降级，zhparser 作为可选增强 |

### 10.2 产品风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **用户不信任自动记忆** | 高 | 透明化 + 可审核 + 可删除 |
| **隐私担忧** | 高 | 严格隔离 + 数据加密 + 合规审计 |
| **记忆过载** | 中 | 重要性排序 + 自动归档 + 手动清理 |
| **误导性记忆** | 高 | 置信度标注 + 来源追溯 + 快速修正 |

### 10.3 运营风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **成本超预算** | 高 | 分阶段上线 + 灰度测试 + 成本监控 |
| **迁移数据丢失** | 中 | 充分测试 + 备份 + 回滚方案 |
| **用户学习成本** | 中 | 渐进式引导 + 文档 + 默认值优化 |

## 11. 监控与评估

### 11.1 关键指标

**功能指标**：
- 记忆提炼成功率（目标 > 90%）
- 记忆检索召回率（目标 > 70%，通过人工评估）
- 摘要质量评分（目标 > 4/5，用户反馈）

**性能指标**：
- 记忆检索 p95 延迟（目标 < 500ms）
- 摘要生成 p95 延迟（目标 < 10s）
- 数据库查询 p95 延迟（目标 < 100ms）

**成本指标**：
- 平均每 Task 的 Token 消耗（目标降低 30%）
- LLM API 调用成本（目标不增加或略增）
- 存储成本（目标 < $10/月）

**用户体验指标**：
- 用户主动创建记忆的比例（目标 > 5%）
- 记忆被检索使用的比例（目标 > 50%）
- 用户对记忆功能的满意度（目标 > 4/5）

### 11.2 A/B 测试方案

- **对照组**：不启用记忆功能，使用完整对话历史
- **实验组**：启用记忆压缩和检索
- **关键指标**：Token 消耗、回答质量（用户评分）、任务完成率

## 12. 附录

### 12.1 参考资料

- **MemGPT 论文**：[MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)
- **LangChain Memory**：[LangChain Memory Documentation](https://python.langchain.com/docs/modules/memory/)
- **OpenAI Assistants API**：[Assistants API Guide](https://platform.openai.com/docs/assistants/overview)
- **Anthropic Prompt Caching**：[Prompt Caching Guide](https://docs.anthropic.com/claude/docs/prompt-caching)

### 12.2 术语表

| 术语 | 定义 |
|------|------|
| **工作记忆** | 当前对话的最近 N 轮消息，直接注入 Prompt |
| **会话摘要** | 对当前会话的压缩总结，定期更新 |
| **长期记忆** | 跨会话保留的结构化记忆，按需检索 |
| **记忆提炼** | 从对话中抽取值得长期保留的信息 |
| **记忆检索** | 根据当前问题查找相关记忆的过程 |
| **混合检索** | 结合关键词、向量、全文搜索的检索策略 |
| **置信度** | 记忆准确性的估计值（0.0-1.0） |
| **重要性** | 记忆对未来任务的价值估计（0.0-1.0） |

### 12.3 示例数据

**记忆记录示例**：

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "owner_user_id": "user123",
  "organization_id": "org456",
  "memory_type": "fact",
  "scope": "conversation",
  "title": "青云官网部署在阿里云",
  "content": "青云官网使用 Docker Compose 部署在阿里云 ECS 上，使用 Nginx 作为反向代理，域名为 qingyun.example.com，SSL 证书由 Let's Encrypt 自动管理。",
  "content_hash": "9f3f5d2a4c0e4a0f8f7d6c5b4a3928172635445362718091a2b3c4d5e6f70819",
  "memory_key": "deploy:qingyun:aliyun",
  "keywords": ["青云官网", "阿里云", "Docker Compose", "Nginx"],
  "source_conversation_id": "c123",
  "source_message_ids": ["m001", "m002"],
  "extraction_method": "llm_extract",
  "confidence": 0.95,
  "importance": 0.85,
  "access_count": 12,
  "last_accessed_at": "2026-10-02T15:30:00Z",
  "status": "active",
  "created_at": "2026-10-01T10:00:00Z",
  "updated_at": "2026-10-02T15:30:00Z"
}
```

**会话摘要示例**：

```json
{
  "conversation_id": "c123",
  "title": "青云官网部署问题",
  "summary": "[主题] 青云官网部署状态确认\n\n[进展] 用户询问官网部署情况，助手查询后确认已部署在阿里云，使用 Docker Compose + Nginx + Let's Encrypt 方案。\n\n[关键信息]\n- 部署平台：阿里云 ECS\n- 域名：qingyun.example.com\n- SSL：Let's Encrypt 自动续期\n\n[待办] 下周检查日志轮转配置",
  "summary_until_message_id": "550e8400-e29b-41d4-a716-446655440008",
  "summary_version": 2,
  "summary_updated_at": "2026-10-02T12:00:00Z",
  "summary_token_count": 450
}
```

## 13. 总结

本方案设计了一套渐进式、可控的记忆机制，核心特点：

1. **分阶段推进**：先做会话内上下文与摘要，再做会话内记忆，最后才启用跨会话记忆
2. **检索渐进启用**：Phase 1 关键词 + simple FTS；向量检索延后到独立索引阶段
3. **RAG 协同**：复用向量基础设施，明确分工边界
4. **渐进实施**：分 5 个 Phase，每个阶段独立验收
5. **成本可控**：估算可节省 30-60% Token 消耗，存储成本极低
6. **用户透明**：记忆可见、可管理、可删除，满足隐私要求

**下一步行动**：
1. 评审本方案，确认技术路线和优先级
2. 启动 Phase 1：summary 边界、ConversationContext 和上下文注入
3. Phase 1 稳定后再创建 memory_records；向量记忆延后
4. 建立监控和评估体系，为后续迭代提供数据支持

---

**文档版本控制**：
- v1.6 (2026-10-03)：写入 Phase 1/2 完成状态与 Phase 3+ 启动阻塞。
  明确本轮只交付后端记忆闭环，记忆管理 API 暂不配套独立页面；
  Phase 3 前补 extraction job，Phase 4 前拆分跨会话存储和向量命名空间；
  pg_trgm 保持非阻塞增强项。
- v1.5 (2026-10-03)：写入 Phase 1/2 开工决议。新增 conversation_summary_jobs；
  明确摘要模型、预算和近似 token 口径；Phase 2 收敛为 conversation-only
  最小 API；session/global/preference 暂不启用；keywords + simple FTS 为必选，
  pg_trgm 权限确认后作为增强。
- v1.4 (2026-10-03)：补齐上下文注入接口契约；工作记忆排除摘要边界前和当前 Task
  消息；summary_until_message_id 增加外键和失效规则；Phase 1 明确不做摘要历史；
  Phase 2 禁止 preference；candidate 增加 job 级幂等；统一轮次口径和版本标识。
- v1.3 (2026-10-03)：统一 Phase 1/2 会话隔离基线。补齐 candidate/deleted 状态、
  conversation scope 的 API 与检索过滤、候选记忆增量提炼、摘要版本表说明、
  访问日志异步统计、向量索引 conversation 过滤，并把跨会话收益从当前验收中移除。
- v1.2 (2026-10-03)：按代码与数据库现状校正。Phase 1 收敛为“会话内上下文与摘要”，
  明确不创建 memory_records、不启用向量；摘要边界改为
  `summary_until_message_id + summary_version CAS`；删除 conversation 使用级联；
  记忆检索 query 纳入最近消息和摘要；中文检索改用 simple/pg_trgm；RAG 向量记忆
  改为 Phase 4 的独立索引能力。
- v1.1 (2026-10-03)：Phase 4 收敛为同一 Conversation 内的记忆检索与压缩，
  暂不启用跨会话偏好、组织记忆和长期记忆注入，并保留 scope/status/检索接口扩展点。
- v1.0 (2026-10-03)：初始版本，完整设计方案
