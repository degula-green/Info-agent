# Agent 记忆机制与压缩方案

## 文档状态

- 版本：v1.0
- 状态：设计阶段
- 作者：架构师（Claude）
- 创建日期：2026-10-03
- 目标：为 Agent 服务设计完整的记忆机制和记忆压缩方案
- 范围：Agent 服务、数据库 schema、与 RAG 服务协同

## 1. 背景与现状分析

### 1.1 现有系统状况

当前 Agent 服务已经实现了基础的对话历史功能（Phase 1 & Phase 2 已完成）：

**已有基础设施**：
- `agent.conversations` 表：会话窗口，包含 `summary` 和 `summary_cursor` 字段（预留）
- `agent.messages` 表：用户可见消息（user/assistant/system）
- `agent.agent_tasks` 表：执行单位，已关联 `conversation_id`
- Conversation API：支持创建、列表、获取、更新、删除会话
- 与 RAG 服务协同：知识检索通过 `services/rag/` 提供

**现有局限**：
1. **无记忆机制**：Agent 每次回答都需要加载完整的对话历史，没有提炼和压缩
2. **上下文爆炸**：长对话会导致 token 消耗指数级增长
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

## 3. 数据库设计

### 3.1 记忆表 (memory_records)

```sql
CREATE TABLE agent.memory_records (
    memory_id uuid PRIMARY KEY,
    owner_user_id text NOT NULL,  -- 沿用现有 TEXT 类型
    organization_id text,
    
    -- 记忆分类
    memory_type varchar(32) NOT NULL,  -- fact, preference, decision, relation, context
    scope varchar(32) NOT NULL,         -- global, conversation, session
    
    -- 记忆内容
    title varchar(200) NOT NULL,        -- 简短标题
    content text NOT NULL,              -- 记忆正文
    keywords text[] DEFAULT '{}',       -- 关键词数组，用于快速过滤
    
    -- 来源追溯
    source_conversation_id uuid REFERENCES agent.conversations(conversation_id) ON DELETE SET NULL,
    source_message_ids uuid[] DEFAULT '{}',  -- 提炼自哪些消息
    extraction_method varchar(32),      -- manual, auto_summary, llm_extract
    
    -- 质量与置信度
    confidence numeric(4,3) NOT NULL DEFAULT 0.800,  -- 0.000 ~ 1.000
    importance numeric(4,3) NOT NULL DEFAULT 0.500,  -- 重要性评分
    
    -- 访问统计
    access_count int NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    
    -- 生命周期
    status varchar(32) NOT NULL DEFAULT 'active',  -- active, archived, superseded
    superseded_by_memory_id uuid REFERENCES agent.memory_records(memory_id) ON DELETE SET NULL,
    expires_at timestamptz,                        -- 可选的过期时间
    
    -- 向量化（预留，实际向量可能存在 RAG）
    embedding_model varchar(64),
    embedding_version int,
    
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    
    CONSTRAINT memory_type_chk 
        CHECK (memory_type IN ('fact', 'preference', 'decision', 'relation', 'context')),
    CONSTRAINT memory_scope_chk 
        CHECK (scope IN ('global', 'conversation', 'session')),
    CONSTRAINT memory_status_chk 
        CHECK (status IN ('active', 'archived', 'superseded')),
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

-- 全文搜索索引（可选）
CREATE INDEX memory_content_fts_idx 
    ON agent.memory_records USING gin (to_tsvector('chinese', content));
```

### 3.2 会话摘要扩展

现有 `agent.conversations` 表已经预留了字段，补充设计：

```sql
-- 现有字段说明
-- summary: text                    -- 会话的压缩摘要
-- summary_cursor: integer          -- 已总结到第几条消息

-- 建议新增字段（可选，在后续迁移中加入）
ALTER TABLE agent.conversations 
    ADD COLUMN IF NOT EXISTS summary_updated_at timestamptz,
    ADD COLUMN IF NOT EXISTS summary_method varchar(32) DEFAULT 'incremental',
    ADD COLUMN IF NOT EXISTS summary_token_count int DEFAULT 0;

COMMENT ON COLUMN agent.conversations.summary IS '会话压缩摘要，每 N 轮或超过阈值时更新';
COMMENT ON COLUMN agent.conversations.summary_cursor IS '已总结到的消息序号（按 created_at 排序）';
COMMENT ON COLUMN agent.conversations.summary_method IS 'incremental: 增量更新, full: 完全重写';
COMMENT ON COLUMN agent.conversations.summary_token_count IS '摘要的 token 数量估算';
```

### 3.3 记忆访问日志（可选，用于优化）

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

## 4. 记忆压缩策略

### 4.1 会话摘要生成

**触发时机**：
1. **轮次阈值**：每 5 轮对话触发一次增量更新
2. **Token 阈值**：历史消息总 token 数超过 8K
3. **手动触发**：用户或系统管理员手动请求

**压缩算法**：

```python
# 伪代码
def update_conversation_summary(conversation_id: str) -> str:
    """增量更新会话摘要"""
    conversation = store.get_conversation(conversation_id)
    cursor = conversation.summary_cursor
    current_summary = conversation.summary or ""
    
    # 获取新消息
    new_messages = store.list_messages_since_cursor(conversation_id, cursor)
    if len(new_messages) < 5:  # 不足 5 条，不值得更新
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
    
    # 更新数据库
    conversation.summary = new_summary
    conversation.summary_cursor = new_messages[-1].sequence_number
    conversation.summary_updated_at = utcnow()
    store.save_conversation(conversation)
    
    return new_summary
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
def build_working_memory(conversation_id: str, max_tokens: int = 4000) -> list[Message]:
    """构建工作记忆：最近的消息 + 摘要"""
    conversation = store.get_conversation(conversation_id)
    messages = store.list_messages(conversation_id, order_by="created_at DESC")
    
    # 1. 始终包含会话摘要（如果有）
    context = []
    token_count = 0
    
    if conversation.summary:
        summary_msg = Message(
            role="system",
            content=f"[会话摘要]\n{conversation.summary}"
        )
        context.append(summary_msg)
        token_count += estimate_tokens(conversation.summary)
    
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

### 4.3 长期记忆提炼

**提炼时机**：
- Task 完成后，异步分析会话内容
- 用户主动标记（"记住这个"）
- 定期扫描高质量对话

**提炼规则**：

```python
def extract_long_term_memories(conversation_id: str) -> list[MemoryRecord]:
    """从会话中提炼长期记忆"""
    messages = store.list_messages(conversation_id)
    
    prompt = f"""
分析以下对话，提炼出值得长期保留的记忆。

对话内容：
{format_messages(messages)}

请提炼以下类型的记忆（JSON 格式）：
1. fact: 稳定的事实信息（项目状态、技术栈、部署环境）
2. preference: 用户偏好（喜欢简洁回答、不喜欢冗长解释）
3. decision: 技术决策（为什么选择某方案、权衡了什么）
4. relation: 人物关系（谁负责什么、团队结构）

要求：
- 每条记忆必须有明确的标题和内容
- 标注置信度（0.0-1.0）和重要性（0.0-1.0）
- 如果没有值得保留的记忆，返回空数组

JSON Schema:
{{
  "memories": [
    {{
      "memory_type": "fact",
      "title": "青云官网部署在阿里云",
      "content": "青云官网使用 Docker Compose 部署在阿里云 ECS 上，域名为 qingyun.example.com，使用 Nginx 反向代理和 Let's Encrypt SSL 证书。",
      "keywords": ["青云官网", "阿里云", "Docker", "部署"],
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
            scope="global",
            title=item["title"],
            content=item["content"],
            keywords=item["keywords"],
            source_conversation_id=conversation_id,
            extraction_method="llm_extract",
            confidence=item["confidence"],
            importance=item["importance"],
            status="active",
            created_at=utcnow(),
            updated_at=utcnow()
        )
        memories.append(memory)
        store.create_memory(memory)
    
    return memories
```

## 5. 记忆检索机制

### 5.1 混合检索策略

结合多种检索方式，按优先级组合：

```python
def retrieve_relevant_memories(
    owner_user_id: str,
    query: str,
    conversation_id: str | None = None,
    top_k: int = 5
) -> list[MemoryRecord]:
    """混合检索相关记忆"""
    
    # 1. 全局偏好记忆（始终加载）
    preference_memories = store.list_memories(
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
            status="active"
        )
    
    # 3. 关键词匹配（快速过滤）
    keywords = extract_keywords(query)
    keyword_memories = store.search_memories_by_keywords(
        owner_user_id=owner_user_id,
        keywords=keywords,
        limit=top_k * 2
    )
    
    # 4. 向量语义检索（复用 RAG 服务）
    query_embedding = rag_client.embed_text(query)
    vector_memories = rag_client.search_memories(
        owner_user_id=owner_user_id,
        embedding=query_embedding,
        top_k=top_k * 2
    )
    
    # 5. 全文搜索（PostgreSQL FTS，补充）
    fts_memories = store.fulltext_search_memories(
        owner_user_id=owner_user_id,
        query=query,
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
    
    # 7. 更新访问统计
    for memory in all_memories[:top_k]:
        memory.access_count += 1
        memory.last_accessed_at = utcnow()
        store.update_memory_stats(memory)
    
    return all_memories[:top_k]
```

### 5.2 记忆排序算法

```python
def calculate_memory_relevance(
    memory: MemoryRecord,
    query_embedding: list[float],
    current_time: datetime
) -> float:
    """计算记忆的综合相关性得分"""
    
    # 1. 语义相似度 (0.0 - 1.0)
    semantic_score = cosine_similarity(memory.embedding, query_embedding)
    
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
| **索引** | 独立的向量数据库 | 复用 RAG 向量索引 |
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
    
    # 2. 个人记忆：用户偏好和相关记忆
    personal_memories = retrieve_relevant_memories(
        owner_user_id=owner_user_id,
        query=query,
        conversation_id=conversation_id,
        top_k=5
    )
    
    # 3. 知识检索：RAG 服务（如果需要）
    knowledge_chunks = []
    if should_search_knowledge(query):
        knowledge_chunks = rag_client.search(
            query=query,
            owner_user_id=owner_user_id,
            top_k=10
        )
    
    # 4. 组装上下文（优先级：偏好 > 工作记忆 > 个人记忆 > 知识）
    context = {
        "preferences": [m for m in personal_memories if m.memory_type == "preference"],
        "working_memory": working_memory,
        "relevant_memories": [m for m in personal_memories if m.memory_type != "preference"],
        "knowledge_chunks": knowledge_chunks
    }
    
    return context
```

### 6.3 向量索引复用

为了避免重复建设向量存储，记忆的向量化可以复用 RAG 服务的基础设施：

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
                "memory_type": metadata["memory_type"],
                "importance": metadata["importance"],
                "created_at": metadata["created_at"]
            }
        )
        
        return doc_id
    
    def search_memories(
        self,
        owner_user_id: str,
        embedding: list[float],
        top_k: int = 10
    ) -> list[dict]:
        """从向量数据库检索记忆"""
        results = self.vector_store.search(
            vector=embedding,
            filter={
                "type": "agent_memory",
                "owner_user_id": owner_user_id
            },
            top_k=top_k
        )
        
        return results
```

## 7. API 设计

### 7.1 记忆管理 API

```text
# 创建记忆（手动）
POST /api/agent/v1/memories
Body: {
  "memory_type": "fact",
  "title": "项目使用 FastAPI 框架",
  "content": "当前项目后端采用 FastAPI 框架...",
  "keywords": ["FastAPI", "后端", "框架"],
  "importance": 0.8
}

# 列出记忆
GET /api/agent/v1/memories?memory_type=preference&page=1&page_size=20

# 获取单个记忆
GET /api/agent/v1/memories/{memory_id}

# 更新记忆
PATCH /api/agent/v1/memories/{memory_id}
Body: {
  "title": "更新后的标题",
  "confidence": 0.9
}

# 删除记忆
DELETE /api/agent/v1/memories/{memory_id}

# 搜索记忆
GET /api/agent/v1/memories/search?q=FastAPI&top_k=10

# 批量导出记忆
GET /api/agent/v1/memories/export?format=json
```

### 7.2 会话摘要 API

```text
# 触发会话摘要更新
POST /api/agent/v1/conversations/{conversation_id}/summarize
Response: {
  "conversation_id": "uuid",
  "summary": "更新后的摘要...",
  "summary_cursor": 15,
  "token_count": 450
}

# 获取会话摘要历史（可选）
GET /api/agent/v1/conversations/{conversation_id}/summary-history
```

### 7.3 记忆提炼 API

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

## 8. 实施计划

### Phase 1：基础记忆存储（2 周）

**目标**：建立记忆数据模型和基础 CRUD

- [ ] 创建数据库迁移：`memory_records` 表
- [ ] 实现 `PostgresAgentStore` 的记忆相关方法
- [ ] 实现记忆管理 API（创建、查询、更新、删除）
- [ ] 添加记忆管理的单元测试和集成测试
- [ ] 前端添加记忆管理界面（可选，可推迟到 Phase 3）

**验收标准**：
- 可以手动创建和管理记忆
- 记忆严格按 owner 隔离
- 支持按类型、关键词过滤

### Phase 2：会话摘要与压缩（2 周）

**目标**：实现会话摘要的自动生成和更新

- [ ] 扩展 `conversations` 表（新增 summary 相关字段）
- [ ] 实现增量摘要生成算法
- [ ] 添加摘要触发逻辑（轮次阈值、Token 阈值）
- [ ] 实现滑动窗口的工作记忆加载
- [ ] 修改 Planner/Answer 输入，注入会话摘要
- [ ] 添加摘要 API 和测试

**验收标准**：
- 长对话自动生成摘要
- 摘要质量可读，长度可控
- Planner 能正确使用摘要上下文

### Phase 3：长期记忆提炼（3 周）

**目标**：从对话中自动提炼长期记忆

- [ ] 实现记忆提炼算法（LLM-based）
- [ ] 添加 Task 完成后的异步提炼任务
- [ ] 实现记忆去重和版本管理（superseded_by）
- [ ] 添加记忆质量评估和置信度调整
- [ ] 前端展示提炼的记忆并支持确认/拒绝

**验收标准**：
- Task 完成后自动提炼记忆
- 记忆质量满足最低阈值（人工抽样评估）
- 用户可审核和管理自动提炼的记忆

### Phase 4：记忆检索与注入（2 周）

**目标**：在 Task 执行时检索并注入相关记忆

- [ ] 实现关键词检索
- [ ] 与 RAG 服务集成，实现向量检索
- [ ] 实现混合检索和排序算法
- [ ] 修改 Planner/Answer 输入，注入检索的记忆
- [ ] 添加记忆访问日志（可选）
- [ ] 性能测试和优化

**验收标准**：
- Planner 能检索到相关记忆
- 记忆检索延迟 < 500ms (p95)
- 记忆注入不显著增加 Token 消耗（< 1K tokens）

### Phase 5：记忆管理与优化（2 周）

**目标**：优化记忆质量和用户体验

- [ ] 实现记忆的时间衰减和过期策略
- [ ] 添加记忆的批量导出/导入
- [ ] 前端记忆可视化界面（图谱或列表）
- [ ] 添加记忆统计和分析面板
- [ ] 用户隐私：支持一键删除所有记忆
- [ ] 成本分析和优化

**验收标准**：
- 用户可以方便地查看和管理记忆
- 记忆质量有明显提升（通过 A/B 测试）
- Token 消耗在预算范围内

## 9. 成本与性能估算

### 9.1 Token 消耗估算

| 场景 | 无记忆（当前） | 有记忆（Phase 2+4） | 节省 |
|------|---------------|-------------------|------|
| 10 轮短对话 | ~3K tokens | ~2K tokens (摘要 + 最近 3 轮) | 33% |
| 50 轮长对话 | ~15K tokens | ~5K tokens (摘要 + 最近 5 轮 + 记忆) | 67% |
| 跨会话复用 | 0 tokens | ~1K tokens (长期记忆) | N/A |

**估算假设**：
- 每条消息平均 150 tokens
- 摘要压缩率 5:1
- 长期记忆平均 200 tokens/条，注入 3-5 条

**预期效果**：
- **短期**：前 10 轮无明显优势，10 轮后开始节省
- **长期**：对话越长，节省越明显，50 轮以上节省 > 60%
- **跨会话**：新会话可以直接使用长期记忆，减少重复询问

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
  "scope": "global",
  "title": "青云官网部署在阿里云",
  "content": "青云官网使用 Docker Compose 部署在阿里云 ECS 上，使用 Nginx 作为反向代理，域名为 qingyun.example.com，SSL 证书由 Let's Encrypt 自动管理。",
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
  "summary_cursor": 8,
  "summary_updated_at": "2026-10-02T12:00:00Z",
  "summary_token_count": 450
}
```

## 13. 总结

本方案设计了一套渐进式、可控的记忆机制，核心特点：

1. **三层记忆架构**：工作记忆（滑动窗口）+ 会话摘要（增量压缩）+ 长期记忆（跨会话检索）
2. **混合检索策略**：关键词 + 向量 + 全文搜索，确保召回率
3. **RAG 协同**：复用向量基础设施，明确分工边界
4. **渐进实施**：分 5 个 Phase，每个阶段独立验收
5. **成本可控**：估算可节省 30-60% Token 消耗，存储成本极低
6. **用户透明**：记忆可见、可管理、可删除，满足隐私要求

**下一步行动**：
1. 评审本方案，确认技术路线和优先级
2. 启动 Phase 1：数据库迁移和基础 CRUD
3. 并行进行：前端原型设计、LLM prompt 调优
4. 建立监控和评估体系，为后续迭代提供数据支持

---

**文档版本控制**：
- v1.0 (2026-10-03)：初始版本，完整设计方案
