# 树形RAG设计方案

## 文档状态

- **版本**: v1.0
- **状态**: 设计阶段
- **创建日期**: 2024-01-XX
- **作者**: 架构设计讨论
- **目标**: 为Agent产品设计树形结构的RAG系统，解决海量数据场景下的检索效率和召回准确率问题

## 1. 背景与问题

### 1.1 产品背景

这是一个行政助理/私人秘书型Agent产品，主要功能包括：

- **表单自动填写**: 从知识库检索用户信息，自动填充表单
- **联系人管理**: 维护和查询人物画像、联系方式
- **周报生成**: 根据本周信息生成周报
- **智能回复**: 根据用户习惯和历史记录回复消息
- **政策查询**: 查找公司服务申报条件的政策
- **知识问答**: 基于群聊、文档、公司资料的检索问答

**数据来源**:
- 飞书、微信等即时通讯群聊消息
- 文档附件（合同、政策、报告）
- 通讯录信息（联系人、公司、项目）
- 用户与Agent的对话历史

### 1.2 核心问题

传统单层RAG架构在信息量增大后面临的问题：

#### 问题1: 主题串扰导致的召回不准确

**典型场景**:
```
上下文1（第50-60条消息）：
"A项目需要升级服务器"
"服务器配置：8核16G"
"IP地址是192.168.1.100"

上下文2（第200-210条消息）：
"B项目也要部署了"
"服务器已经申请好了，16核32G"
"测试环境的服务器密码是xxx"

用户查询："A项目的服务器信息"
```

**传统RAG的问题**:
- 向量检索会同时召回两个上下文中的"服务器"相关消息
- 无法区分这些消息分别属于A项目还是B项目
- 召回结果混杂，噪音比例高达60%+

**根本原因**:
- 传统RAG只有**语义相似度**这一个维度
- 不理解**归属关系**（这条消息属于哪个主题/实体）
- Chunk孤立存储，丢失了上下文中的隐式主题信息

#### 问题2: 隐式主题无法识别

群聊场景中，大多数消息是隐式主题：

```
显式主题（好处理）：
"A项目的服务器配置是8核16G"  ← 明确提到"A项目"

隐式主题（传统RAG的噩梦）：
[上文一直在聊A项目]
"服务器配置：8核16G"  ← 没有提"A项目"
"还要申请域名"         ← 没有提"A项目"
"预算大概5000块"       ← 没有提"A项目"
```

人们对话时不会每句话都重复主题，但传统RAG的Chunk是孤立的，无法推断归属关系。

#### 问题3: 检索效率低

- 全库检索（百万级chunks）速度慢
- 即使做了索引优化，仍需要扫描大量无关数据
- 无法利用"归属关系"快速过滤

### 1.3 设计目标

**核心目标**: 解决归属关系缺失导致的主题串扰问题

**具体目标**:
1. **提高召回准确率**: 查询"A项目"时，不召回"B项目"的信息
2. **加快检索速度**: 先定位实体节点，再在节点内检索，避免全库扫描
3. **支持隐式主题**: 通过上下文窗口识别，将隐式消息正确归属
4. **保持降级能力**: 树结构失效时，能降级到传统RAG
5. **延时生成**: 消息先走传统RAG保证可用性，定时扫描生成树结构

## 2. 核心设计理念

### 2.1 树+图混合结构

**设计原则**:
```
树形：用于"定位和归属"
  - 垂直查找：快速定位实体和其chunks
  - 层级清晰：便于管理和展示
  - 隔离性好：不同实体的chunks天然隔离

网状：用于"关系和扩展"
  - 横向查找：发现相关实体
  - 灵活连接：表达多对多关系
  - 推理能力：通过关系链推断
```

### 2.2 为什么是树+图，而不是纯树？

**问题**: 组织结构本质上是网状的

```
纯树形（有局限）：
A公司
  ├─ A员工
  ├─ A项目
  └─ A政策

问题：
- 查询"A员工" → 必须知道"A公司"才能找到
- 查询"A项目参与人" → 只能向上找到A公司，找不到A员工
```

**解决方案**: 扁平的节点 + 网状的关系

```
第一层（实体类型，固定）：
├─ Organizations
├─ Persons  
├─ Projects
├─ Policies
└─ Contracts

第二层（具体实体，扁平）：
Organizations/A公司
Persons/A员工
Projects/A项目
Policies/某政策

第三层（Chunks）：
各实体节点下挂载的消息片段

关系网络（横向连接）：
A员工 ←works_for→ A公司
A员工 ←participates_in→ A项目  
A项目 ←belongs_to→ A公司
```

**优势**:
- ✅ 直接查"A员工" → Persons/A员工节点，不需要知道公司
- ✅ 查"A员工的公司" → 通过关系边找到A公司
- ✅ 查"A项目的参与人" → 通过关系边找到A员工
- ✅ 保持树的层级清晰（只有2层实体）
- ✅ 用关系网络补充树无法表达的横向连接

## 3. 完整架构设计

### 3.1 树形结构

```
┌─────────────────────────────────────────────────────────┐
│                    用户空间 (Root)                         │
│              user_id / organization_id                   │
└─────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
   【第一层：实体类型】 (固定5类)
        │                   │                   │
┌───────▼────────┐  ┌──────▼─────┐  ┌─────▼──────┐
│ Organizations  │  │  Persons   │  │  Projects  │
│   (公司/组织)   │  │  (联系人)   │  │   (项目)    │
└───────┬────────┘  └──────┬─────┘  └─────┬──────┘
        │                   │               │
   【第二层：具体实体】 (扁平化，不再嵌套)
        │                   │               │
    ┌───▼────┐          ┌──▼───┐      ┌───▼────┐
    │ A公司  │          │ 张三  │      │ A项目  │
    │ B公司  │          │ 李四  │      │ B项目  │
    └───┬────┘          └──┬───┘      └───┬────┘
        │                  │               │
   【第三层：Chunks】
        │                  │               │
    ┌───▼────────┐    ┌───▼─────┐   ┌────▼────┐
    │ chunk_001  │    │chunk_050│   │chunk_100│
    │ chunk_002  │    │chunk_051│   │chunk_101│
    │ chunk_003  │    │chunk_052│   │chunk_102│
    └────────────┘    └─────────┘   └─────────┘

┌─────────────────────────────────────────────────────────┐
│       Policies (政策)           Contracts (合同)         │
│          ├─ 高新技术企业认定       ├─ 2024技术服务合同   │
│          └─ 研发费用加计扣除       └─ XX项目合作协议      │
└─────────────────────────────────────────────────────────┘
```

### 3.2 关系网络

```
┌─────────────────────────────────────────────────────────┐
│              关系网络 (横向，存在关系表)                    │
└─────────────────────────────────────────────────────────┘

关系类型定义：

Person 相关：
  - works_for: 人 → 公司
  - participates_in: 人 → 项目
  - contacts: 人 ↔ 人

Project 相关：
  - belongs_to: 项目 → 公司
  - governed_by: 项目 → 政策
  - has_member: 项目 → 人

Contract 相关：
  - signed_by: 合同 → 公司/人
  - related_to: 合同 → 项目

Policy 相关：
  - applies_to: 政策 → 公司/项目

示例关系：
  张三 ─works_for─→ A公司
  张三 ─participates_in─→ A项目
  A项目 ─belongs_to─→ A公司
  A项目 ─governed_by─→ 高新技术企业认定政策
  2024技术服务合同 ─signed_by─→ A公司
  2024技术服务合同 ─related_to─→ A项目
```

**关系约束**:
- 只允许预定义的关系类型（避免关系爆炸）
- 关系遍历深度限制为2跳（避免性能问题）
- 不同关系有权重（强关系优先展开）

## 4. 数据模型设计

### 4.1 实体表 (entities)

```sql
CREATE TABLE entities (
    entity_id UUID PRIMARY KEY,
    
    -- 归属
    owner_user_id TEXT NOT NULL,
    organization_id TEXT,
    
    -- 实体信息
    entity_type VARCHAR(32) NOT NULL,  -- organization, person, project, policy, contract
    canonical_name VARCHAR(200) NOT NULL,  -- 规范全称
    normalized_key VARCHAR(200) NOT NULL,  -- 规范化键（用于匹配）
    aliases TEXT[] DEFAULT '{}',           -- 别名列表
    keywords TEXT[] DEFAULT '{}',          -- 关键词
    description TEXT,                       -- 描述（可选，增强语义）
    
    -- 语义向量（用于语义检索）
    embedding vector(1536),
    
    -- 统计信息
    chunk_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_mentioned_at TIMESTAMPTZ,
    
    -- 索引
    CONSTRAINT entity_type_chk CHECK (entity_type IN (
        'organization', 'person', 'project', 'policy', 'contract'
    ))
);

-- 索引
CREATE INDEX entities_owner_idx ON entities(owner_user_id, entity_type);
CREATE INDEX entities_type_idx ON entities(entity_type);
CREATE INDEX entities_normalized_idx ON entities(normalized_key);
CREATE INDEX entities_aliases_idx ON entities USING gin(aliases);
CREATE INDEX entities_keywords_idx ON entities USING gin(keywords);
CREATE INDEX entities_name_trgm_idx ON entities USING gin(canonical_name gin_trgm_ops);
CREATE INDEX entities_embedding_idx ON entities 
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 4.2 Chunk表 (chunks)

```sql
CREATE TABLE chunks (
    chunk_id UUID PRIMARY KEY,
    
    -- 内容
    content TEXT NOT NULL,
    embedding vector(1536),  -- 向量
    keywords TEXT[] DEFAULT '{}',
    
    -- 元数据
    conversation_id UUID,
    message_id UUID,
    sent_at TIMESTAMPTZ,
    sender_id TEXT,
    sender_name TEXT,
    
    -- 时间和归属
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX chunks_embedding_idx ON chunks 
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX chunks_keywords_idx ON chunks USING gin(keywords);
CREATE INDEX chunks_sent_at_idx ON chunks(sent_at);
CREATE INDEX chunks_conversation_idx ON chunks(conversation_id);
```

### 4.3 Chunk挂载表 (chunk_mounts)

多对多关系：一个chunk可以挂载到多个实体

```sql
CREATE TABLE chunk_mounts (
    mount_id UUID PRIMARY KEY,
    chunk_id UUID REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    
    -- 挂载元信息
    confidence FLOAT NOT NULL DEFAULT 0.8,  -- 挂载置信度
    mount_method VARCHAR(32) NOT NULL,      -- explicit | window_batch | llm_infer
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(chunk_id, entity_id)
);

-- 索引
CREATE INDEX chunk_mounts_chunk_idx ON chunk_mounts(chunk_id);
CREATE INDEX chunk_mounts_entity_idx ON chunk_mounts(entity_id, confidence);
```

**挂载方式说明**:
- `explicit`: 消息中明确提到实体名称，立即挂载（confidence=0.95）
- `window_batch`: 定时窗口扫描，批量挂载（confidence=0.65-0.85）
- `llm_infer`: LLM推断的隐含关系（confidence=0.50-0.70）

### 4.4 实体关系表 (entity_relations)

```sql
CREATE TABLE entity_relations (
    relation_id UUID PRIMARY KEY,
    
    source_entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    target_entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL,
    
    -- 关系元信息
    confidence FLOAT NOT NULL DEFAULT 0.8,
    evidence_chunk_ids UUID[] DEFAULT '{}',  -- 支撑该关系的chunk
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(source_entity_id, target_entity_id, relation_type)
);

-- 索引
CREATE INDEX entity_relations_source_idx ON entity_relations(source_entity_id, relation_type);
CREATE INDEX entity_relations_target_idx ON entity_relations(target_entity_id, relation_type);
```

## 5. 检索策略设计

### 5.1 三层检索架构

```
Layer 1: 实体定位
  目标：从用户查询中识别实体，定位到树节点
  方法：精确匹配 + 语义检索 + 上下文推断
  
Layer 2: Chunk检索
  目标：在限定的实体节点内检索相关消息
  方法：向量检索 + 关键词检索（混合）
  
Layer 3: 关系查询
  目标：通过关系网络扩展相关实体
  方法：图遍历（限制深度）
```

### 5.2 Layer 1: 实体定位（精确 + 语义混合）

#### 问题场景

```
用户查询："aims信息"
实体库中：Projects/AIMS系统开发项目

挑战：
- 用户用简称"aims"
- 实体全称是"AIMS系统开发项目"
- 纯精确匹配会失败
```

#### 解决方案：三阶段识别

```python
def locate_entities(query: str, user_id: str, conversation_id: str):
    """
    三阶段实体识别：精确优先 + 语义兜底 + 上下文推断
    """
    matched = []
    
    # ===== 阶段1：精确匹配（5-10ms）=====
    # 适用于：用户说全称，或别名已维护
    candidates = extract_entity_mentions(query)
    
    for candidate in candidates:
        # 1.1 完全匹配
        entity = db.query(
            "SELECT * FROM entities WHERE canonical_name ILIKE ? OR ? = ANY(aliases)",
            candidate, candidate
        )
        if entity:
            matched.append({**entity, "score": 1.0, "method": "exact"})
            continue
        
        # 1.2 模糊匹配（pg_trgm）
        entity = db.query(
            """SELECT *, similarity(canonical_name, ?) as score 
               FROM entities 
               WHERE similarity(canonical_name, ?) > 0.5
               ORDER BY score DESC LIMIT 1""",
            candidate, candidate
        )
        if entity and entity.score > 0.7:
            matched.append({**entity, "method": "fuzzy"})
            continue
    
    # 如果精确匹配置信度够高，直接返回
    if matched and max(e.score for e in matched) > 0.9:
        return matched
    
    # ===== 阶段2：语义检索（20-50ms）=====
    # 2.1 限定候选范围（关键优化！）
    candidate_pool = []
    
    # 优先级1：当前对话提到过的实体（最相关）
    recent_entities = db.query(
        """SELECT DISTINCT e.* FROM entities e
           JOIN chunk_mounts cm ON e.entity_id = cm.entity_id
           JOIN chunks c ON cm.chunk_id = c.chunk_id
           WHERE c.conversation_id = ?
             AND c.sent_at > NOW() - INTERVAL '1 day'""",
        conversation_id
    )
    candidate_pool.extend(recent_entities)
    
    # 优先级2：用户空间的活跃实体
    if len(candidate_pool) < 100:
        user_entities = db.query(
            """SELECT * FROM entities 
               WHERE owner_user_id = ?
                 AND last_mentioned_at > NOW() - INTERVAL '3 months'""",
            user_id
        )
        candidate_pool.extend(user_entities)
    
    # 2.2 向量相似度匹配
    if candidate_pool:
        query_embedding = embed(query)
        semantic_matches = []
        
        for entity in candidate_pool:
            similarity = cosine_similarity(query_embedding, entity.embedding)
            if similarity > 0.75:  # 高阈值
                semantic_matches.append({
                    **entity,
                    "score": similarity,
                    "method": "semantic"
                })
        
        semantic_matches.sort(key=lambda x: x["score"], reverse=True)
        matched.extend(semantic_matches[:3])
    
    # ===== 阶段3：上下文推断（50-100ms）=====
    if not matched or max(e.score for e in matched) < 0.7:
        # 从最近对话中推断用户指的是哪个实体
        recent_entities = get_entities_from_recent_messages(
            conversation_id, lookback=10
        )
        
        if recent_entities:
            # LLM判断消歧
            inferred = llm_infer_entity_reference(
                query=query,
                recent_entities=recent_entities
            )
            if inferred and inferred.confidence > 0.8:
                matched.append({**inferred, "method": "context"})
    
    return dedupe_and_rank(matched)
```

**关键优化**:

1. **范围限定**：不在全库10万实体中检索，而是：
   - 先查当前对话的实体（50个）
   - 再查用户空间的活跃实体（1000个）
   - 大幅降低计算量

2. **实体Embedding**：
   ```python
   # 为每个实体生成综合向量
   entity_text = f"{canonical_name} {' '.join(aliases)} {description}"
   entity.embedding = embed(entity_text)
   ```
   好处：即使别名没维护，"aims"的向量和"AIMS系统"的向量也很接近

3. **置信度分级**：
   ```
   explicit_match: 0.95  # 精确匹配
   fuzzy_match: 0.80     # 模糊匹配
   semantic_match: 0.75  # 语义匹配
   context_infer: 0.70   # 上下文推断
   ```

### 5.3 Layer 2: Chunk检索（向量 + 关键词混合）

#### 为什么需要混合检索？

**纯向量检索的问题**：
```
查询："服务器IP地址"
Chunk1："服务器IP是192.168.1.100"  ← 应该召回
Chunk2："项目进度良好，系统稳定"    ← 不应该召回（但向量可能匹配）

向量检索：两者都可能召回（都跟"服务器"有关）
关键词匹配：只召回Chunk1（精确匹配"IP"）
```

**纯关键词检索的问题**：
```
查询："项目进度"
Chunk："已完成60%，预计下月上线"  ← 应该召回（描述的就是进度）

关键词匹配：匹配不上（没有"进度"二字）
向量检索：能匹配上（语义相关）
```

#### 混合检索流程

```python
def retrieve_chunks_from_entity(
    entity_id: str, 
    query: str, 
    top_k: int = 10
):
    """
    在指定实体节点内，混合检索相关chunks
    """
    # Step 1: 获取该实体的所有chunk
    entity_chunk_ids = db.query(
        """SELECT chunk_id FROM chunk_mounts 
           WHERE entity_id = ? AND confidence > 0.65""",
        entity_id
    )
    
    # Step 2: 向量检索（主路径）
    query_embedding = embed(query)
    vector_results = db.query(
        """SELECT chunk_id, content, 
                  1 - (embedding <=> ?) as similarity
           FROM chunks
           WHERE chunk_id = ANY(?)
           ORDER BY embedding <=> ?
           LIMIT ?""",
        query_embedding, entity_chunk_ids, query_embedding, top_k * 2
    )
    
    # Step 3: 关键词检索（补充路径）
    keywords = extract_keywords(query)
    keyword_results = db.query(
        """SELECT chunk_id, content,
                  ts_rank(to_tsvector('simple', content), 
                         plainto_tsquery('simple', ?)) as rank
           FROM chunks
           WHERE chunk_id = ANY(?)
             AND keywords && ?
           ORDER BY rank DESC
           LIMIT ?""",
        query, entity_chunk_ids, keywords, top_k
    )
    
    # Step 4: RRF融合排序
    fused = reciprocal_rank_fusion(
        vector_results, 
        keyword_results,
        weights={"vector": 0.7, "keyword": 0.3}
    )
    
    return fused[:top_k]
```

**关键点**：
- 先限定范围（entity_chunk_ids），不是全库检索
- 向量检索捕捉语义相似
- 关键词检索保证精确命中
- RRF融合两者优势

### 5.4 Layer 3: 关系查询（图遍历）

```python
def find_related_entities(
    entity_id: str,
    relation_type: str = None,
    direction: str = "outbound",  # outbound | inbound | both
    max_depth: int = 1
):
    """
    查找相关实体（限制深度）
    """
    if direction == "outbound":
        # A员工 → 找ta参与的项目
        return db.query(
            """SELECT e.* FROM entities e
               JOIN entity_relations r ON e.entity_id = r.target_entity_id
               WHERE r.source_entity_id = ?
                 AND (r.relation_type = ? OR ? IS NULL)
                 AND r.confidence > 0.6
               ORDER BY r.confidence DESC""",
            entity_id, relation_type, relation_type
        )
    
    elif direction == "inbound":
        # A项目 → 找参与的人
        return db.query(
            """SELECT e.* FROM entities e
               JOIN entity_relations r ON e.entity_id = r.source_entity_id
               WHERE r.target_entity_id = ?
                 AND (r.relation_type = ? OR ? IS NULL)
               ORDER BY r.confidence DESC""",
            entity_id, relation_type, relation_type
        )
```

**为什么用PostgreSQL而不是图数据库？**
- 关系深度有限（1-2跳）
- 关系类型固定（10种左右）
- PostgreSQL的递归CTE足够
- 运维成本低（不需要额外维护Neo4j）

## 6. 完整检索流程

### 6.1 端到端流程

```python
def smart_search(query: str, user_id: str, conversation_id: str, top_k: int = 10):
    """
    树+图混合检索的完整流程
    """
    
    # ========== Step 1: 实体定位 ==========
    entities = locate_entities(query, user_id, conversation_id)
    # 返回: [{entity_id, name, score, method}, ...]
    
    if not entities:
        # 没有识别到实体，降级到传统RAG
        return traditional_rag_search(query, user_id, top_k)
    
    all_chunks = []
    
    # ========== Step 2: 树检索（直接） ==========
    for entity in entities:
        direct_chunks = retrieve_chunks_from_entity(
            entity_id=entity.entity_id,
            query=query,
            top_k=top_k
        )
        all_chunks.extend([
            {**chunk, "source": "direct", "entity": entity.name}
            for chunk in direct_chunks
        ])
        
        # ========== Step 3: 图扩展（如果不够） ==========
        if len(direct_chunks) < top_k * 0.5:
            related_entities = find_related_entities(
                entity_id=entity.entity_id,
                max_depth=1
            )
            
            for related in related_entities[:3]:
                related_chunks = retrieve_chunks_from_entity(
                    entity_id=related.entity_id,
                    query=query,
                    top_k=top_k // 2
                )
                all_chunks.extend([
                    {**chunk, "source": "related", "entity": related.name}
                    for chunk in related_chunks
                ])
    
    # ========== Step 4: 元数据过滤 ==========
    # 时间过滤（不放在树里，用元数据）
    time_range = extract_time_range(query)
    if time_range:
        all_chunks = [
            chunk for chunk in all_chunks
            if time_range.start <= chunk.sent_at <= time_range.end
        ]
    
    # 群聊过滤
    conversation_filter = extract_conversation_filter(query)
    if conversation_filter:
        all_chunks = [
            chunk for chunk in all_chunks
            if chunk.conversation_id in conversation_filter
        ]
    
    # ========== Step 5: 排序去重 ==========
    deduplicated = dedupe_by_chunk_id(all_chunks)
    ranked = rank_by_relevance(deduplicated, query)
    
    return ranked[:top_k]
```

### 6.2 检索场景示例

#### 场景1：直接查询实体

```
Query: "张三的联系方式"

Step 1 - 实体识别:
  primary_entity = "张三" (Persons类型)

Step 2 - 定位节点:
  node = Persons/张三

Step 3 - 检索chunks:
  在该节点下检索 "联系方式"
  
Result: 只返回张三相关的chunks（电话、邮箱等）
召回准确率：95%+（不会混入其他人的信息）
```

#### 场景2：关系查询

```
Query: "张三参与了哪些项目？"

Step 1 - 实体识别:
  primary_entity = "张三"

Step 2 - 关系查找:
  relations = find_relations(张三, relation_type="participates_in")
  result = [A项目, B项目]

Step 3 - 节点展开:
  for project in result:
    get_chunks(project, query="参与 任务 进度")

Result: 张三在各项目的参与记录
```

#### 场景3：跨实体查询

```
Query: "A公司正在进行的项目"

Step 1 - 实体识别:
  primary_entity = "A公司"

Step 2 - 关系展开:
  projects = find_relations(A公司, relation_type="owns")
  result = [A项目, C项目]

Step 3 - 状态过滤:
  for project in result:
    status_chunks = get_chunks(project, query="进度 状态")
    filter by "正在进行"

Result: 只返回进行中的项目
```

## 7. Chunk挂载策略

### 7.1 窗口批量挂载（核心机制）

#### 为什么需要窗口批量？

群聊消息大多是隐式主题：

```
[消息1] "今天讨论一下A项目"  ← 显式
[消息2] "目前进度到哪了？"    ← 隐式
[消息3] "已经完成了60%"       ← 隐式
[消息4] "服务器配置：8核16G"  ← 隐式（但很重要！）
[消息5] "预算还够吗？"         ← 隐式
```

如果只挂载显式消息，大量关键信息会丢失。

#### 定时扫描流程

```python
def scan_and_mount_conversations():
    """
    每小时执行一次，扫描群聊并批量挂载
    """
    # 1. 获取过去1小时的所有群聊消息
    messages = get_recent_messages(hours=1)
    
    # 2. 按会话分组，每组取滑动窗口
    for conversation in group_by_conversation(messages):
        windows = sliding_window(
            conversation, 
            size=20,   # 窗口大小：20条消息
            step=10    # 步长：10条（50%重叠）
        )
        
        for window in windows:
            # 3. LLM识别窗口主题实体
            result = llm_extract_entities_and_relations(window)
            # 返回: {
            #   entities: [
            #     {name: "A项目", type: "project", confidence: 0.9},
            #     {name: "张三", type: "person", confidence: 0.7}
            #   ],
            #   relations: [
            #     {source: "张三", target: "A项目", type: "participates_in"}
            #   ]
            # }
            
            # 4. 批量挂载chunks
            for msg in window:
                for entity in result.entities:
                    if entity.confidence > 0.65:  # 宁可漏，不要错
                        mount_chunk_to_entity(
                            chunk_id=msg.chunk_id,
                            entity_id=entity.id,
                            confidence=entity.confidence,
                            method="window_batch"
                        )
            
            # 5. 更新实体关系
            for rel in result.relations:
                if rel.confidence > 0.7:
                    upsert_entity_relation(
                        source=rel.source,
                        target=rel.target,
                        relation_type=rel.type,
                        evidence_chunk_ids=[msg.chunk_id for msg in window]
                    )
```

#### 窗口参数选择

```
推荐配置：
- size: 20条消息
- step: 10条（50%重叠）
- 扫描频率: 每小时

原因：
- 太小(5条)：上下文不足，识别不准
- 太大(50条)：容易跨越多个话题
- 重叠(50%)：避免边界消息被漏掉
```

### 7.2 挂载置信度分级

```python
MOUNT_CONFIDENCE = {
    "explicit": 0.95,        # 消息明确提到实体
    "window_strong": 0.85,   # 窗口主题明确，消息相关性强
    "window_weak": 0.65,     # 窗口主题明确，消息相关性弱
    "inferred": 0.50         # LLM推断的隐含关系
}

# 检索时可以按场景调整阈值
def retrieve(query, min_confidence=0.7):
    # 严格场景（表单填写）：只用高置信度 >=0.85
    # 宽松场景（探索式查询）：允许低置信度 >=0.65
    pass
```

### 7.3 多实体挂载

一个chunk可以同时挂载到多个实体：

```python
# 示例消息
msg = "张三负责A项目的服务器升级，预算5000元"

# 应该同时挂载到：
mount_chunk_to_entity(chunk_id, entity_id="张三", confidence=0.95)
mount_chunk_to_entity(chunk_id, entity_id="A项目", confidence=0.95)
mount_chunk_to_entity(chunk_id, entity_id="服务器", confidence=0.70)  # 可选
```

**好处**：
- 查"张三负责什么" → 从Persons/张三入口找到
- 查"A项目的任务" → 从Projects/A项目入口找到
- 两个查询能找到**同一条消息的不同侧面**

## 8. 性能预估

### 8.1 延迟分析

#### 场景1：单实体查询
```
Query: "A项目的服务器配置"

Layer 1: 实体定位
  - 精确匹配：5ms（B-tree索引）
  - 未找到 → 语义检索：30ms（限定在1000个实体内）

Layer 2: Chunk检索  
  - A项目节点有1000条chunks
  - 向量检索（ivfflat）：30ms
  - 关键词检索（GIN）：10ms
  - 融合排序：5ms

总耗时：~80ms（传统全库检索需要200ms+）
```

#### 场景2：多实体+关系扩展
```
Query: "张三参与的项目进度"

Layer 1: 实体定位：5ms（精确匹配"张三"）
Layer 2: 关系查询：10ms（找到3个项目）
Layer 3: Chunk检索（3个项目）：100ms
总耗时：~115ms
```

### 8.2 召回率提升

**传统RAG**：
```
查询："A项目的服务器信息"
全库检索 → 召回100条
- A项目相关：40条
- B项目相关：30条
- 其他项目：30条

噪音比例：60%
```

**树形RAG**：
```
查询："A项目的服务器信息"
1. 定位到A项目节点（1000条chunks）
2. 只在这1000条中检索
3. 召回100条，都是A项目相关

噪音比例：<5%（只有挂载错误的chunk）
```

### 8.3 存储成本

```
假设数据量：
- 实体：10万个
  - 公司：1万
  - 人：3万
  - 项目：2万
  - 政策/合同：4万
- Chunks：1000万条
- Chunk挂载：3000万条（平均每chunk挂3个实体）
- 关系：50万条

存储占用：
- entities表：10万 × 1KB = 100MB
- chunks表：1000万 × (1KB内容 + 6KB向量) = 70GB
- chunk_mounts表：3000万 × 100B = 3GB
- entity_relations表：50万 × 200B = 100MB
- 索引（向量为主）：~30GB

总计：~103GB（可接受）
```

## 9. 实施计划

### Phase 1: 基础树结构（4周）

**目标**: 建立实体-chunk的基本树结构

**交付**:
- [ ] 数据库schema设计和迁移
- [ ] entities、chunks、chunk_mounts表创建
- [ ] 基本的实体识别（精确匹配）
- [ ] 单实体节点的chunk检索
- [ ] 降级到传统RAG的逻辑

**验收**:
- 能识别显式提到的实体（"A项目的信息"）
- 能在实体节点内检索chunks
- 检索速度 < 100ms

### Phase 2: 窗口批量挂载（3周）

**目标**: 实现定时扫描和批量挂载

**交付**:
- [ ] 滑动窗口扫描逻辑
- [ ] LLM实体提取prompt和解析
- [ ] 批量挂载worker
- [ ] 挂载置信度管理

**验收**:
- 隐式消息能被正确挂载
- 窗口扫描不影响实时检索
- 挂载准确率 > 85%

### Phase 3: 语义实体识别（2周）

**目标**: 支持简称和语义匹配

**交付**:
- [ ] 实体embedding生成
- [ ] 范围限定的语义检索
- [ ] 别名自动发现
- [ ] 上下文推断逻辑

**验收**:
- 能识别简称（"aims" → "AIMS项目"）
- 能处理口语化表达（"那个ai项目"）
- 实体识别召回率 > 90%

### Phase 4: 关系网络（2周）

**目标**: 建立实体间的关系图

**交付**:
- [ ] entity_relations表和索引
- [ ] 关系提取逻辑
- [ ] 关系查询API
- [ ] 关系扩展检索

**验收**:
- 支持关系查询（"张三参与的项目"）
- 关系提取准确率 > 80%
- 关系查询延迟 < 50ms

### Phase 5: 优化与监控（持续）

**目标**: 性能优化和质量监控

**交付**:
- [ ] 检索性能监控
- [ ] 挂载质量评估
- [ ] 树退化检测和处理
- [ ] A/B测试框架

## 10. 风险与挑战

### 10.1 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **实体识别准确率低** | 高 | 精确+语义+上下文三阶段兜底 |
| **窗口挂载噪音大** | 中 | 高置信度阈值（0.65+）+ 人工抽检 |
| **实体去重困难** | 中 | normalized_key + embedding相似度 |
| **关系提取错误** | 中 | 要求多条证据支持 + 置信度 |
| **PostgreSQL向量性能** | 中 | 范围限定 + ivfflat索引优化 |
| **LLM调用成本高** | 中 | 批量处理 + 缓存 + 采样评估 |

### 10.2 数据质量风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **实体名称不规范** | 高 | 别名维护 + 自动发现 + 用户纠正 |
| **窗口话题切换** | 中 | 检测切换标记 + 缩短窗口 |
| **同名实体冲突** | 中 | 用户空间隔离 + 上下文消歧 |
| **历史数据挂载** | 低 | 离线批处理 + 优先处理近期数据 |

### 10.3 产品风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **用户不理解树结构** | 低 | 前端透明化，用户无感 |
| **检索结果解释性差** | 中 | 返回来源实体和挂载路径 |
| **冷启动问题** | 中 | 前期允许降级到传统RAG |

## 11. 监控指标

### 11.1 功能指标

- **实体识别召回率**: 目标 > 90%
- **实体识别准确率**: 目标 > 85%
- **窗口挂载准确率**: 目标 > 85%
- **检索噪音比例**: 目标 < 10%（传统RAG为60%）

### 11.2 性能指标

- **实体定位延迟 p95**: 目标 < 50ms
- **Chunk检索延迟 p95**: 目标 < 100ms
- **端到端检索延迟 p95**: 目标 < 150ms

### 11.3 质量指标

- **用户查询满意度**: 目标 > 4/5
- **检索结果相关性**: 目标 > 85%（人工评估）
- **树结构覆盖率**: 活跃实体覆盖率 > 80%

## 12. 与现有系统的关系

### 12.1 不改变的部分

- **RAG服务边界**: 继续负责chunk解析、embedding、索引
- **Knowledge服务**: 继续管理知识库、权限、同步
- **Agent服务**: 继续负责任务执行、能力调度

### 12.2 新增的部分

- **实体管理服务**: 负责实体识别、关系提取、别名维护
- **窗口扫描Worker**: 定时扫描群聊，批量挂载chunks
- **树检索路由**: 在传统RAG前增加树路由层

### 12.3 迁移策略

- **渐进式**: 新消息走树结构，旧消息保留在传统RAG
- **AB测试**: 部分用户先开启树结构，评估效果
- **降级开关**: 树结构异常时自动降级到传统RAG

## 13. 总结

### 13.1 核心价值

**解决的核心问题**:
- 传统RAG的主题串扰（A项目和B项目混淆）
- 隐式主题的归属识别（"服务器配置"属于哪个项目）
- 海量数据下的检索效率（先定位节点，再检索）

**设计亮点**:
- 树+图混合结构（树负责归属，图负责关系）
- 实体扁平化（不是多层嵌套，避免查询路径依赖）
- 精确+语义混合识别（速度与准确性兼顾）
- 窗口批量挂载（解决隐式主题问题）
- 多实体挂载（一条消息从多个角度可达）

**预期效果**:
- 检索噪音从60%降到<10%
- 检索速度提升50%+（范围限定）
- 支持复杂查询（关系查询、多实体）

### 13.2 下一步行动

1. **评审本方案**，确认技术路线和优先级
2. **启动Phase 1**：基础树结构和数据模型
3. **准备测试数据**：标注部分群聊的实体和关系
4. **建立评估体系**：召回率、准确率、延迟监控

---

## 14. 技术栈选择

### 14.1 方案对比总览

| 方案 | 技术栈 | 优势 | 劣势 | 推荐度 |
|------|--------|------|------|--------|
| **方案A** | PostgreSQL + ES | 各取所长、改动小、一致性好 | 两套系统 | ⭐⭐⭐⭐⭐ |
| **方案B** | PostgreSQL全家桶 | 统一技术栈、运维简单、成本低 | 放弃ES投资、BM25不如ES | ⭐⭐⭐⭐ |
| **方案C** | PostgreSQL + Neo4j | 图查询强大 | 多技术栈、数据同步问题 | ⭐⭐⭐ |
| **方案D** | PostgreSQL + Milvus | 向量检索极快 | 过度设计、同步成本高 | ⭐⭐ |

### 14.2 推荐方案：PostgreSQL管理树图 + Elasticsearch检索

#### 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                   数据层分工                              │
├─────────────────────────────────────────────────────────┤
│                                                           │
│  ┌──────────────────────┐      ┌────────────────────┐  │
│  │  PostgreSQL          │      │  Elasticsearch     │  │
│  │  (权威数据源)         │ ───→ │  (检索引擎)        │  │
│  │                      │ 同步  │                    │  │
│  │  - entities          │      │  - chunks索引      │  │
│  │  - entity_relations  │      │  - BM25检索        │  │
│  │  - chunk_mounts      │      │  - kNN向量检索     │  │
│  │  - 元数据管理         │      │  - RRF融合         │  │
│  └──────────────────────┘      └────────────────────┘  │
│                                                           │
└─────────────────────────────────────────────────────────┘
```

#### 职责分工

**PostgreSQL（结构化数据 + 关系管理）**：

```sql
-- 实体表
CREATE TABLE entities (
    entity_id UUID PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    canonical_name VARCHAR(200) NOT NULL,
    normalized_key VARCHAR(200) NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    embedding vector(1536),  -- 实体语义向量
    -- 树结构字段
    path TEXT,  -- 路径枚举：Organizations/A公司
    depth INT,  -- 深度
    -- 统计信息
    chunk_count INT DEFAULT 0,
    last_mentioned_at TIMESTAMPTZ
);

-- 关系表（图的边）
CREATE TABLE entity_relations (
    relation_id UUID PRIMARY KEY,
    source_entity_id UUID NOT NULL,
    target_entity_id UUID NOT NULL,
    relation_type VARCHAR(32) NOT NULL,
    confidence FLOAT DEFAULT 0.8,
    evidence_chunk_ids UUID[] DEFAULT '{}',
    FOREIGN KEY (source_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY (target_entity_id) REFERENCES entities(entity_id)
);

-- Chunk挂载表（多对多）
CREATE TABLE chunk_mounts (
    mount_id UUID PRIMARY KEY,
    chunk_id UUID NOT NULL,
    entity_id UUID NOT NULL,
    confidence FLOAT NOT NULL DEFAULT 0.8,
    mount_method VARCHAR(32) NOT NULL,
    UNIQUE(chunk_id, entity_id)
);

-- 关键索引
CREATE INDEX entities_type_idx ON entities(entity_type);
CREATE INDEX entities_path_idx ON entities(path);
CREATE INDEX entities_embedding_idx ON entities USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX relations_source_idx ON entity_relations(source_entity_id, relation_type);
CREATE INDEX chunk_mounts_entity_idx ON chunk_mounts(entity_id, confidence);
```

**Elasticsearch（全文检索 + 向量检索）**：

```json
// chunks索引（保持现有结构）
PUT /chunks
{
  "mappings": {
    "properties": {
      "chunk_id": {"type": "keyword"},
      "content": {"type": "text"},
      "embedding": {
        "type": "dense_vector",
        "dims": 1536,
        "index": true,
        "similarity": "cosine"
      },
      "keywords": {"type": "keyword"},
      "sent_at": {"type": "date"},
      "conversation_id": {"type": "keyword"}
    }
  }
}
```

#### 查询流程

```python
# services/rag/app/application/tree_rag_service.py

class TreeRAGService:
    def __init__(self, pg_store, es_client):
        self.pg = pg_store      # PostgreSQL连接
        self.es = es_client      # Elasticsearch客户端
    
    def search(self, query: str, user_id: str, top_k: int = 10):
        """
        树形RAG检索：PG管理结构，ES提供检索
        """
        # ===== Step 1: 实体定位（PostgreSQL）=====
        entities = self._locate_entities_pg(query, user_id)
        
        if not entities:
            # 降级：传统全库检索
            return self._fallback_es_search(query, top_k)
        
        # ===== Step 2: 树查询（PostgreSQL）=====
        # 获取这些实体的所有chunks
        chunk_ids = self._get_entity_chunks_pg(entities)
        
        if not chunk_ids:
            return []
        
        # ===== Step 3: 限定范围检索（Elasticsearch）=====
        # 在限定的chunk_ids范围内，做BM25 + kNN混合检索
        results = self._es_search_in_scope(
            query=query,
            chunk_ids=chunk_ids,
            top_k=top_k
        )
        
        return results
    
    def _locate_entities_pg(self, query: str, user_id: str):
        """
        实体定位：精确匹配 + 模糊匹配 + 语义匹配
        """
        # 阶段1：精确匹配（B-tree索引，5ms）
        exact = self.pg.query(
            """SELECT entity_id, canonical_name, 1.0 AS score, 'exact' AS method
               FROM entities 
               WHERE (canonical_name ILIKE %s OR %s = ANY(aliases))
                 AND owner_user_id = %s
               LIMIT 1""",
            f"%{query}%", query, user_id
        )
        if exact:
            return exact
        
        # 阶段2：模糊匹配（pg_trgm，10ms）
        fuzzy = self.pg.query(
            """SELECT entity_id, canonical_name, 
                      similarity(canonical_name, %s) AS score,
                      'fuzzy' AS method
               FROM entities
               WHERE canonical_name %% %s  -- pg_trgm相似度
                 AND owner_user_id = %s
               ORDER BY score DESC
               LIMIT 3""",
            query, query, user_id
        )
        if fuzzy and fuzzy[0].score > 0.7:
            return fuzzy
        
        # 阶段3：语义匹配（pgvector，限定范围后30ms）
        query_embedding = embed(query)
        semantic = self.pg.query(
            """SELECT entity_id, canonical_name,
                      1 - (embedding <=> %s::vector) AS score,
                      'semantic' AS method
               FROM entities
               WHERE owner_user_id = %s
                 AND last_mentioned_at > NOW() - INTERVAL '3 months'
               ORDER BY embedding <=> %s::vector
               LIMIT 3""",
            query_embedding, user_id, query_embedding
        )
        return semantic if semantic and semantic[0].score > 0.75 else []
    
    def _get_entity_chunks_pg(self, entities):
        """
        树查询：获取实体节点下的所有chunks
        """
        entity_ids = [e.entity_id for e in entities]
        
        chunk_ids = self.pg.query(
            """SELECT DISTINCT chunk_id 
               FROM chunk_mounts
               WHERE entity_id = ANY(%s)
                 AND confidence > 0.65""",
            entity_ids
        )
        return [row.chunk_id for row in chunk_ids]
    
    def _es_search_in_scope(self, query: str, chunk_ids: list, top_k: int):
        """
        限定范围检索：在指定chunks中做BM25 + kNN混合检索
        """
        query_embedding = embed(query)
        
        # 利用ES现有能力：BM25 + kNN + RRF
        response = self.es.search(
            index="chunks",
            body={
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [
                            {"ids": {"values": chunk_ids}},  # 范围限定！
                            {"match": {"content": query}}     # BM25
                        ]
                    }
                },
                "knn": {
                    "field": "embedding",
                    "query_vector": query_embedding,
                    "k": top_k,
                    "num_candidates": min(len(chunk_ids), 100),
                    "filter": {"ids": {"values": chunk_ids}}  # 范围限定！
                }
            }
        )
        
        return response["hits"]["hits"]
    
    def _fallback_es_search(self, query: str, top_k: int):
        """
        降级：传统全库检索（保持原有能力）
        """
        return self.es.search(
            index="chunks",
            body={
                "size": top_k,
                "query": {"match": {"content": query}},
                "knn": {
                    "field": "embedding",
                    "query_vector": embed(query),
                    "k": top_k,
                    "num_candidates": 100
                }
            }
        )
```

#### 关系查询示例

```python
def search_with_relations(self, query: str, user_id: str, expand_relations: bool = True):
    """
    支持关系扩展的检索
    """
    # Step 1: 实体定位
    entities = self._locate_entities_pg(query, user_id)
    
    if not entities:
        return self._fallback_es_search(query, 10)
    
    # Step 2: 收集chunks（直接）
    direct_chunk_ids = self._get_entity_chunks_pg(entities)
    
    all_chunk_ids = set(direct_chunk_ids)
    
    # Step 3: 关系扩展（可选）
    if expand_relations and len(direct_chunk_ids) < 50:
        for entity in entities:
            # 查询相关实体（1跳）
            related = self.pg.query(
                """SELECT DISTINCT e.entity_id
                   FROM entity_relations r
                   JOIN entities e ON r.target_entity_id = e.entity_id
                   WHERE r.source_entity_id = %s
                     AND r.confidence > 0.7
                   LIMIT 5""",
                entity.entity_id
            )
            
            # 获取相关实体的chunks
            for rel_entity in related:
                rel_chunks = self._get_entity_chunks_pg([rel_entity])
                all_chunk_ids.update(rel_chunks)
    
    # Step 4: ES检索（限定范围）
    return self._es_search_in_scope(query, list(all_chunk_ids), 10)
```

#### 数据同步策略

**方案A：保持独立（推荐）**

```python
# Chunks在ES中独立存储
# PG只存储结构化信息（entities, relations, mounts）
# 两者通过chunk_id关联

优点：
- 无需同步chunks内容
- ES继续承担现有职责
- PG专注于结构管理

缺点：
- 需要跨系统查询
```

**方案B：定时同步元数据（可选）**

```python
# 如果需要在ES中过滤实体
# 可以将实体信息同步到ES的chunk文档

def sync_entity_info_to_es():
    """
    每小时同步：将chunk的实体标签同步到ES
    """
    # 查询最近挂载的chunks
    recent_mounts = pg.query(
        """SELECT cm.chunk_id, array_agg(e.entity_id) as entity_ids
           FROM chunk_mounts cm
           JOIN entities e ON cm.entity_id = e.entity_id
           WHERE cm.updated_at > NOW() - INTERVAL '1 hour'
           GROUP BY cm.chunk_id"""
    )
    
    # 批量更新ES
    bulk_body = []
    for mount in recent_mounts:
        bulk_body.append({"update": {"_id": mount.chunk_id}})
        bulk_body.append({"doc": {"entity_ids": mount.entity_ids}})
    
    es.bulk(index="chunks", body=bulk_body)

# ES中可以直接过滤
GET /chunks/_search
{
  "query": {
    "bool": {
      "must": [
        {"terms": {"entity_ids": ["A项目_id"]}},  # 直接过滤
        {"match": {"content": "服务器"}}
      ]
    }
  }
}
```

### 14.3 为什么不用Elasticsearch管理树+图？

#### 核心问题：ES不擅长关系查询

Elasticsearch是**文档数据库**，不支持JOIN：

```json
// ES的困境示例
// 场景：查询"张三参与的项目中，讨论服务器的消息"

// PostgreSQL（高效）
SELECT c.*
FROM entity_relations r1
JOIN entity_relations r2 ON r1.target_entity_id = r2.source_entity_id
JOIN chunk_mounts cm ON r2.target_entity_id = cm.entity_id
JOIN chunks c ON cm.chunk_id = c.chunk_id
WHERE r1.source_entity_id = '张三'
  AND r1.relation_type = 'participates_in';
-- 执行时间：<100ms

// Elasticsearch（困难）
// 第1次查询：张三参与的项目
GET /relations/_search
{
  "query": {
    "bool": {
      "must": [
        {"term": {"source_entity_id": "张三"}},
        {"term": {"relation_type": "participates_in"}}
      ]
    }
  }
}
// 返回：[项目A, 项目B]

// 第2次查询：这些项目的chunks
GET /chunks/_search
{
  "query": {
    "bool": {
      "must": [
        {"terms": {"entities.entity_id": ["项目A", "项目B"]}},
        {"match": {"content": "服务器"}}
      ]
    }
  }
}

// 问题：
// 1. 需要2次网络往返（延迟增加）
// 2. 应用层需要拼接逻辑
// 3. 如果项目列表很长，第2次查询会很大
```

#### 数据冗余和一致性问题

如果用ES管理树+图，需要反规范化：

```json
// Chunk文档需要冗余存储实体信息
{
  "chunk_id": "xxx",
  "content": "张三负责A项目的服务器",
  "embedding": [...],  // 6KB
  "entities": [  // 冗余！
    {
      "entity_id": "张三",
      "entity_name": "张三",
      "entity_type": "person",
      "aliases": ["小张", "张工"]
    },
    {
      "entity_id": "A项目",
      "entity_name": "AIMS系统开发项目",
      "entity_type": "project"
    }
  ],  // 额外2-3KB
  "relations": [  // 冗余！
    {"source": "张三", "target": "A项目", "type": "participates_in"}
  ]
}

// 问题：实体名称更新时
// 需要批量更新所有相关的chunk文档（可能10万条）
POST /chunks/_update_by_query
{
  "script": {
    "source": "..."  // 复杂的脚本更新
  }
}
// 耗时：>10秒，且无法保证原子性
```

**对比PostgreSQL**：

```sql
-- 实体名称更新
BEGIN;
  UPDATE entities SET canonical_name = '新名称' WHERE entity_id = 'xxx';
COMMIT;
-- 耗时：<5ms
-- 关联的chunks自动生效（通过JOIN查询）
```

#### 性能对比

| 场景 | PostgreSQL | Elasticsearch | 说明 |
|------|-----------|---------------|------|
| **树查询** | <10ms | <20ms | ES需要nested查询 |
| **图查询(1跳)** | <20ms | 2次请求~50ms | ES需要应用层拼接 |
| **图查询(2跳)** | <100ms | 3次请求~150ms | ES多次网络往返 |
| **向量检索(限定范围)** | <50ms | <30ms | ES略快 |
| **混合查询(树+图+向量)** | <150ms | >300ms | ES需多次查询拼接 |
| **实体更新** | <5ms | 批量更新>10s | ES需更新所有关联chunks |

### 14.4 为什么不用Neo4j图数据库？

#### Neo4j的优势场景

```cypher
// Neo4j擅长的场景

// 1. 复杂路径查询（3跳以上）
MATCH path = (p:Person {name: '张三'})-[*1..5]-(target)
RETURN path
ORDER BY length(path);

// 2. 社区发现
CALL gds.louvain.stream('myGraph')
YIELD nodeId, communityId;

// 3. 最短路径
MATCH (start:Person {name: '张三'}), (end:Policy {name: '高新技术认证'})
MATCH path = shortestPath((start)-[*]-(end))
RETURN path;
```

#### 你的场景不需要

**原因1：关系深度有限**

```
你的查询：
- 1跳：张三参与的项目（90%的查询）
- 2跳：张三参与的项目所属的公司（10%的查询）
- 3跳+：几乎没有

PostgreSQL递归CTE完全够用：
WITH RECURSIVE relation_chain AS (...)  -- <100ms
```

**原因2：关系类型固定**

```
你只有10种关系类型：
- works_for
- participates_in
- belongs_to
- signed_by
- applies_to
- ...

不需要Neo4j的灵活性
```

**原因3：运维复杂度**

```yaml
Neo4j劣势:
  - 多技术栈：PostgreSQL + ES + Neo4j
  - 数据同步：实体变更需要同步3个系统
  - 学习曲线：团队需要学Cypher查询语言
  - 成本：Neo4j企业版按核心数收费
```

#### 何时考虑Neo4j？

仅当以下条件**同时满足**：
- ✅ 需要3跳以上的复杂图查询
- ✅ 需要图算法（PageRank、最短路径、社区发现）
- ✅ 关系类型动态变化
- ✅ 团队熟悉Neo4j

**你的场景不满足，不推荐。**

### 14.5 为什么不用Milvus/Weaviate专业向量库？

#### 专业向量库的优势

```
Milvus/Weaviate优势：
- 向量检索性能：p99 < 10ms（全库亿级）
- GPU加速：支持GPU加速向量计算
- 分布式：天然支持分片和副本
```

#### 你的场景不需要

**关键：范围已限定**

```python
你的查询流程：
1. 实体定位 → 确定A项目
2. 树查询 → 获取A项目的1000个chunks
3. 向量检索 → 只在这1000个chunks中检索

pgvector处理1000条：<30ms
Milvus处理1000条：<10ms

提升：20ms
代价：增加一个系统 + 数据同步
```

**数据量不大**

```
你的预估：1000万chunks
Milvus适用场景：1亿+ chunks

pgvector 1000万性能：
- 全库检索：~200ms（但你已范围限定，不需要全库）
- 限定范围(1000条)：~30ms

完全够用！
```

#### 何时考虑Milvus？

仅当以下条件满足：
- ✅ 向量数量 > 5000万
- ✅ 全库检索p99要求 < 50ms（你已经不需要全库检索）
- ✅ 需要GPU加速

**你的场景不满足，过度设计。**

### 14.6 最终推荐：PostgreSQL + Elasticsearch

#### 技术栈

```yaml
数据层:
  PostgreSQL 15+:
    - 扩展：pgvector, pg_trgm, btree_gin
    - 职责：实体管理、关系管理、树结构、元数据
  
  Elasticsearch 8.x:
    - 职责：全文检索(BM25)、向量检索(kNN)、RRF融合
    - 保持现有投资和能力

同步策略:
  - chunks独立存储在ES（无需同步内容）
  - 可选：定时同步实体标签到ES（优化过滤）
```

#### 决策矩阵

| 评估维度 | 权重 | PG+ES | PG全家桶 | PG+Neo4j | PG+Milvus |
|---------|------|-------|---------|----------|-----------|
| **开发效率** | 25% | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ |
| **运维成本** | 25% | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐ |
| **性能** | 20% | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **功能满足** | 20% | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **扩展性** | 10% | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **综合得分** | - | **4.75** | **4.25** | **3.75** | **3.85** |

#### 优势总结

1. **充分利用现有投资**：ES已经在用，不需要替换
2. **各取所长**：PG管理结构（擅长关系），ES做检索（擅长全文+向量）
3. **改动最小**：保持现有RAG能力，增量添加树+图层
4. **一致性保证**：PG的ACID保证数据一致性
5. **运维简单**：团队已熟悉PG+ES，无需学习新系统

---

## 15. 与Agent执行范式的集成

### 15.1 Agent当前架构分析

从 `services/agent/app/kernel/runtime.py` 代码分析，当前Agent采用的是**Plan-Execute-Decide循环**，而非标准的ReAct范式。

#### 标准ReAct范式（对比）

```
标准ReAct:
1. Thought（思考当前状态）
2. Action（执行单个工具）
3. Observation（观察结果）
→ 回到步骤1，继续思考

特点：每次只执行一个工具，然后立即重新思考
```

#### 当前Agent架构

```python
# 核心循环：AgentRuntime._drive()
for iteration in range(max_iterations):
    # 1. 理解任务（首次）
    if need_understanding:
        understanding = planner.understand_task(task)
    
    # 2. 规划（Plan-First）
    if need_planning:
        plan = planner.create_plan(
            task, 
            capabilities, 
            observations
        )
        # 一次性生成多步计划
    
    # 3. 执行就绪步骤（Batch Execute）
    ready_steps = find_ready_steps(plan)
    for step in ready_steps:
        observation = executor.execute(step)
    
    # 4. 决策（Periodic Decide）
    decision = planner.decide(task, plan, observations)
    # action: continue | replan | complete | fail | request_input
    
    # 5. 应用决策
    if decision.action == "complete":
        return success
    elif decision.action == "replan":
        plan = create_new_plan(decision.steps)
    # ...
```

**关键特点**：
1. **Plan-First**：一次性生成多步计划，不是每步都重新思考
2. **Batch Execute**：连续执行多个无依赖步骤
3. **Periodic Decide**：执行完一批步骤后才重新决策
4. **DAG支持**：步骤可以声明依赖关系，支持并行准备

### 15.2 树形RAG与Agent集成策略

#### 策略1：规划阶段识别实体（推荐）

```python
# services/agent/app/planning/tree_enhanced_planner.py

class TreeEnhancedPlanner:
    def __init__(self, base_planner, entity_locator):
        self.base_planner = base_planner
        self.entity_locator = entity_locator
    
    def create_plan(self, task, capabilities, observations):
        """
        在规划阶段就识别实体，注入Plan上下文
        """
        # 1. 提前识别任务中涉及的实体
        entities = self.entity_locator.locate_entities(
            query=task.input.text,
            user_id=task.owner_user_id,
            conversation_id=task.conversation_id
        )
        
        # 2. 调用原有planner
        plan = self.base_planner.create_plan(
            task, capabilities, observations
        )
        
        # 3. 将实体信息注入Plan的上下文
        plan.context = plan.context or {}
        plan.context["identified_entities"] = [
            {
                "entity_id": e.entity_id,
                "entity_name": e.canonical_name,
                "entity_type": e.entity_type,
                "confidence": e.score
            }
            for e in entities
        ]
        
        return plan
```

**优势**：
- ✅ 一次识别，多步复用（多个检索步骤共享实体信息）
- ✅ 减少LLM调用（不需要每步重新识别）
- ✅ 提高一致性（整个Plan使用相同的实体理解）

**使用示例**：

```python
# 用户："帮我填写A公司的申报表单"

# 规划阶段
plan = planner.create_plan(task)
# plan.context = {
#     "identified_entities": [
#         {"entity_id": "company_001", "entity_name": "A公司", "entity_type": "organization"}
#     ]
# }

# 执行阶段
# Step 1: knowledge.search_sources
# 从plan.context中读取entity_id，直接定位树节点

# Step 2: knowledge.search_content  
# 复用相同的entity_id，无需重新识别
```

#### 策略2：批量检索优化

```python
# services/rag/app/application/tree_rag_service.py

def batch_search_for_plan(self, plan, steps):
    """
    为Plan中的多个检索步骤批量定位实体节点
    """
    # 1. 从Plan上下文中读取已识别的实体
    entities = plan.context.get("identified_entities", [])
    
    if not entities:
        # 降级：每步独立检索
        return [self.search(step.query) for step in steps]
    
    # 2. 批量获取所有实体的chunks（一次查询）
    all_entity_ids = [e["entity_id"] for e in entities]
    entity_chunks_map = self.pg.query(
        """SELECT entity_id, array_agg(chunk_id) as chunk_ids
           FROM chunk_mounts
           WHERE entity_id = ANY(%s) AND confidence > 0.65
           GROUP BY entity_id""",
        all_entity_ids
    )
    
    # 3. 为每个步骤分配对应的chunk范围
    results = []
    for step in steps:
        # 确定该步骤需要哪些实体
        step_entities = determine_relevant_entities(step, entities)
        step_chunk_ids = []
        for entity in step_entities:
            step_chunk_ids.extend(entity_chunks_map[entity["entity_id"]])
        
        # 在限定范围内检索
        result = self._es_search_in_scope(
            query=step.query,
            chunk_ids=step_chunk_ids,
            top_k=step.top_k
        )
        results.append(result)
    
    return results
```

#### 策略3：决策阶段的诊断反馈

```python
def decide_with_diagnostics(self, task, plan, observations):
    """
    在决策阶段，提供树形RAG的诊断信息
    """
    decision = self.base_planner.decide(task, plan, observations)
    
    # 如果决策是replan，添加诊断信息
    if decision.action == "replan":
        # 分析最近的检索观察
        last_search = find_last_search_observation(observations)
        
        if last_search:
            diagnostics = {
                "entity_matched": last_search.entity_id is not None,
                "entity_name": last_search.entity_name,
                "chunk_count": last_search.chunk_count,
                "search_method": last_search.method,  # tree | fallback
                "confidence": last_search.confidence
            }
            
            # 根据诊断调整重规划策略
            if diagnostics["entity_matched"] and diagnostics["chunk_count"] == 0:
                # 实体匹配成功，但没有chunks
                decision.reason = "实体找到了，但没有相关信息，需要扩展搜索范围"
                decision.suggestions = ["尝试关系扩展", "降级到全库检索"]
            
            elif not diagnostics["entity_matched"]:
                # 实体匹配失败
                decision.reason = "未能识别查询中的实体"
                decision.suggestions = ["重新理解用户意图", "使用全库检索"]
    
    return decision
```

### 15.3 集成后的完整流程

```python
# 用户查询："A项目的服务器配置"

# ===== Phase 1: 规划 =====
understanding = planner.understand_task(task)
# → "用户想查询A项目的服务器配置信息"

entities = entity_locator.locate_entities(
    query="A项目的服务器配置",
    user_id=user_id
)
# → [{"entity_id": "proj_001", "name": "A项目", "type": "project"}]

plan = planner.create_plan(task, capabilities, observations)
plan.context["identified_entities"] = entities
# Plan生成：
# Step 1: knowledge.search_sources(entity_id="proj_001", query="服务器")
# Step 2: knowledge.search_content(source_refs=Step1.sources, query="配置")

# ===== Phase 2: 执行 =====
# Step 1执行
result = tree_rag.search(
    query="服务器",
    entity_id="proj_001",  # 从Plan上下文中读取
    top_k=10
)
# → 在A项目节点下检索"服务器"
# → 召回10条chunks（都是A项目相关）

# Step 2执行
result = tree_rag.search(
    query="配置",
    entity_id="proj_001",  # 复用相同的entity
    top_k=5
)

# ===== Phase 3: 决策 =====
decision = planner.decide(task, plan, observations)
# 检查：
# - 检索到的信息是否足够？
# - 是否需要关系扩展？
# - 是否需要replan？

if decision.action == "complete":
    return format_answer(observations)
elif decision.action == "replan":
    # 根据诊断信息重新规划
    pass
```

### 15.4 优势总结

**与Plan-Execute-Decide范式的协同**：

1. **规划阶段识别实体**
   - 一次识别，多步复用
   - 降低LLM调用成本
   - 提高实体理解的一致性

2. **批量执行优化**
   - 多个检索步骤共享树节点定位
   - 减少数据库查询次数
   - 提升整体吞吐量

3. **决策阶段诊断**
   - 树形RAG提供详细诊断信息
   - 帮助planner理解检索失败原因
   - 指导replan策略（扩展关系 vs 降级全库）

4. **降级保护**
   - 树结构失效时自动降级
   - 不影响Agent的正常执行
   - 保持传统RAG作为兜底

---

**文档版本控制**:
- v1.0 (2024-01-XX): 初始版本，完整设计方案
- v1.1 (2024-01-XX): 增加技术栈选择章节，增加Agent集成章节
