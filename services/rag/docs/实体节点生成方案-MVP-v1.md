# 实体节点生成方案 - MVP版本

## 文档状态

- **版本**: v1.0
- **状态**: MVP实施阶段
- **创建日期**: 2024-01-XX
- **目标**: 设计最小可行的实体节点生成方案，支持人工审核和未来自动化扩展

## 1. MVP核心原则

### 1.1 设计理念

```yaml
MVP目标:
  - 快速上线: 2周完成核心功能
  - 人工为主: 所有实体由人工确认
  - 保留扩展性: 数据结构支持未来自动化

非目标（MVP不做）:
  - 自动消歧
  - 自动合并
  - 复杂规则引擎
  - 实体推荐
```

### 1.2 核心流程

```
消息到达 → 传统RAG（立即可用）
         ↓
定时任务（每小时）
         ↓
LLM批量提取实体
         ↓
保存为pending状态
         ↓
人工审核界面
         ↓
批准/拒绝/修改
         ↓
active实体用于树形检索
         ↓
【预留】自动合并建议
```

## 2. 数据模型设计

### 2.1 实体表（entities）

```sql
CREATE TABLE entities (
    entity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- 归属
    owner_user_id TEXT NOT NULL,
    organization_id TEXT,
    
    -- 实体信息
    canonical_name VARCHAR(200) NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    attributes JSONB DEFAULT '{}',
    
    -- LLM提取信息（保留用于审核参考）
    extraction_info JSONB DEFAULT '{}',  -- 存储LLM提取的原始信息
    /*
    extraction_info 结构示例:
    {
      "mentions": ["aims", "AIMS项目", "aims系统"],
      "evidence": "对话中讨论了该项目的服务器、进度",
      "llm_suggested_name": "AIMS系统开发项目",
      "confidence": 0.85,
      "extracted_at": "2024-01-15T10:30:00Z",
      "extraction_context": "20条消息窗口"
    }
    */
    
    -- 状态管理
    status VARCHAR(32) DEFAULT 'pending',
    -- pending: 待审核
    -- active: 已激活
    -- rejected: 已拒绝
    -- merged: 已合并到其他实体
    
    reject_reason TEXT,
    merged_into UUID REFERENCES entities(entity_id),
    
    -- 【扩展性】合并建议（预留字段）
    merge_suggestions JSONB DEFAULT '[]',
    /*
    merge_suggestions 结构示例（未来自动化使用）:
    [
      {
        "target_entity_id": "entity_002",
        "confidence": 0.85,
        "reason": "姓氏匹配+部门相同",
        "evidence_chunks": ["chunk_001", "chunk_002"],
        "suggested_at": "2024-01-15T11:00:00Z",
        "status": "pending|accepted|rejected"
      }
    ]
    */
    
    -- 时间戳
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    approved_by TEXT,
    rejected_at TIMESTAMPTZ,
    
    -- 统计信息
    chunk_count INT DEFAULT 0,
    last_mentioned_at TIMESTAMPTZ,
    
    -- 约束
    CONSTRAINT entity_type_chk CHECK (entity_type IN (
        'organization', 'person', 'project', 'policy', 'contract'
    )),
    CONSTRAINT status_chk CHECK (status IN (
        'pending', 'active', 'rejected', 'merged'
    ))
);

-- 索引
CREATE INDEX entities_owner_status_idx ON entities(owner_user_id, status);
CREATE INDEX entities_status_idx ON entities(status) WHERE status = 'pending';
CREATE INDEX entities_type_idx ON entities(entity_type);
CREATE INDEX entities_name_trgm_idx ON entities USING gin(canonical_name gin_trgm_ops);

-- 自动更新时间戳
CREATE TRIGGER update_entities_updated_at
    BEFORE UPDATE ON entities
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
```

### 2.2 实体合并历史表（entity_merge_history）

```sql
-- 【扩展性】记录所有合并操作，支持回滚和分析
CREATE TABLE entity_merge_history (
    merge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    source_entity_id UUID NOT NULL,
    target_entity_id UUID NOT NULL,
    
    -- 合并前的快照
    source_snapshot JSONB NOT NULL,  -- 源实体的完整信息
    target_snapshot_before JSONB NOT NULL,  -- 目标实体合并前的状态
    
    -- 合并决策信息
    merge_type VARCHAR(32) NOT NULL,  -- manual | auto_suggested | auto_confirmed
    confidence FLOAT,
    reason TEXT,
    
    -- 操作信息
    merged_by TEXT NOT NULL,  -- user_id 或 'system'
    merged_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- 【扩展性】自动合并相关
    auto_suggestion_id UUID,  -- 如果是基于自动建议的合并
    
    CONSTRAINT merge_type_chk CHECK (merge_type IN (
        'manual', 'auto_suggested', 'auto_confirmed'
    ))
);

CREATE INDEX merge_history_source_idx ON entity_merge_history(source_entity_id);
CREATE INDEX merge_history_target_idx ON entity_merge_history(target_entity_id);
CREATE INDEX merge_history_time_idx ON entity_merge_history(merged_at DESC);
```

### 2.3 实体关系候选表（entity_relation_candidates）

```sql
-- 【扩展性】存储LLM提取的关系，用于未来自动化
CREATE TABLE entity_relation_candidates (
    candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    source_entity_id UUID NOT NULL,
    target_entity_id UUID NOT NULL,
    relation_type VARCHAR(32) NOT NULL,
    
    confidence FLOAT NOT NULL,
    evidence_chunk_ids UUID[] DEFAULT '{}',
    extraction_info JSONB DEFAULT '{}',
    
    -- 状态
    status VARCHAR(32) DEFAULT 'pending',  -- pending | approved | rejected
    
    -- 审核信息
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT relation_status_chk CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX relation_candidates_status_idx ON entity_relation_candidates(status);
```

## 3. MVP核心代码

### 3.1 LLM实体提取服务

```python
# services/rag/app/application/entity_extraction_service.py

from dataclasses import dataclass
from typing import List, Optional
import json
from datetime import datetime, timezone

@dataclass
class ExtractedEntity:
    name: str
    entity_type: str
    mentions: List[str]
    evidence: str
    confidence: float
    context_window_id: str


class EntityExtractionService:
    """
    MVP版本：纯LLM提取 + 人工审核
    """
    
    def __init__(self, llm_client, db, redis_client):
        self.llm = llm_client
        self.db = db
        self.redis = redis_client
    
    def extract_from_window(
        self, 
        window: List[Message], 
        user_id: str
    ) -> List[ExtractedEntity]:
        """
        从消息窗口提取实体
        """
        # 1. 检查缓存（避免重复提取）
        window_hash = self._hash_window(window)
        cached = self.redis.get(f"entity_extract:{window_hash}")
        if cached:
            return json.loads(cached)
        
        # 2. 格式化上下文
        window_text = self._format_window(window)
        
        # 3. 获取用户已有实体（给LLM参考）
        existing_entities = self._get_user_entities_summary(user_id)
        
        # 4. 调用LLM提取
        extracted = self._llm_extract(window_text, existing_entities)
        
        # 5. 缓存结果
        self.redis.setex(
            f"entity_extract:{window_hash}",
            3600,  # 1小时
            json.dumps([e.__dict__ for e in extracted])
        )
        
        return extracted
    
    def _llm_extract(
        self, 
        window_text: str, 
        existing_entities: List[dict]
    ) -> List[ExtractedEntity]:
        """
        LLM提取实体
        """
        existing_text = self._format_existing_entities(existing_entities)
        
        prompt = f"""
## 任务
从对话中提取实体（公司、人名、项目、政策、合同）。

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

## 输出格式
返回JSON数组：
[
  {{
    "existing_entity_id": "如果匹配到已知实体，填写ID；否则null",
    "name": "规范化名称（或原始提法）",
    "type": "organization|person|project|policy|contract",
    "confidence": 0.0-1.0,
    "mentions": ["对话中的各种叫法"],
    "evidence": "为什么认为这是实体的依据"
  }}
]

如果没有实体，返回[]。
"""
        
        response = self.llm.structured_call(
            prompt, 
            response_schema=EntityListSchema,
            temperature=0  # 确定性输出
        )
        
        return [
            ExtractedEntity(
                name=e["name"],
                entity_type=e["type"],
                mentions=e["mentions"],
                evidence=e["evidence"],
                confidence=e["confidence"],
                context_window_id=self._generate_window_id()
            )
            for e in response
        ]
    
    def save_as_pending(
        self, 
        extracted: List[ExtractedEntity], 
        user_id: str,
        window: List[Message]
    ):
        """
        保存为待审核实体
        """
        for entity in extracted:
            # 检查是否已存在
            existing = self.db.query_one(
                """SELECT entity_id FROM entities 
                   WHERE owner_user_id = %s
                     AND canonical_name ILIKE %s
                     AND status != 'rejected'""",
                user_id, entity.name
            )
            
            if existing:
                # 已存在，增加挂载关系即可
                entity_id = existing.entity_id
            else:
                # 创建新实体
                entity_id = self.db.insert(
                    """INSERT INTO entities (
                        owner_user_id,
                        canonical_name,
                        entity_type,
                        status,
                        extraction_info
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING entity_id""",
                    user_id,
                    entity.name,
                    entity.entity_type,
                    'pending',
                    json.dumps({
                        "mentions": entity.mentions,
                        "evidence": entity.evidence,
                        "llm_suggested_name": entity.name,
                        "confidence": entity.confidence,
                        "extracted_at": datetime.now(timezone.utc).isoformat(),
                        "context_window_id": entity.context_window_id
                    })
                ).entity_id
            
            # 挂载chunks
            self._mount_chunks(entity_id, entity, window)
    
    def _mount_chunks(
        self, 
        entity_id: str, 
        entity: ExtractedEntity, 
        window: List[Message]
    ):
        """
        将窗口中提到该实体的chunks挂载到实体
        """
        for message in window:
            # 检查消息是否提到该实体
            if any(
                mention.lower() in message.content.lower() 
                for mention in entity.mentions
            ):
                self.db.execute(
                    """INSERT INTO chunk_mounts (
                        chunk_id, 
                        entity_id, 
                        confidence, 
                        mount_method
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chunk_id, entity_id) DO NOTHING""",
                    message.chunk_id,
                    entity_id,
                    entity.confidence,
                    'llm_window_batch'
                )


# 定时任务
class EntityExtractionWorker:
    """
    定时扫描消息，提取实体
    """
    
    def run_once(self, batch_size: int = 100):
        """
        处理一批消息窗口
        """
        # 1. 获取最近1小时的消息
        recent_messages = self.db.query(
            """SELECT * FROM chunks 
               WHERE created_at > NOW() - INTERVAL '1 hour'
                 AND processed_for_entities = false
               ORDER BY sent_at ASC
               LIMIT %s""",
            batch_size
        )
        
        if not recent_messages:
            return 0
        
        # 2. 按会话分组
        grouped = self._group_by_conversation(recent_messages)
        
        # 3. 滑动窗口
        processed = 0
        for conversation_id, messages in grouped.items():
            windows = self._sliding_window(messages, size=20, step=10)
            
            for window in windows:
                # 提取实体
                user_id = window[0].owner_user_id
                extracted = self.extraction_service.extract_from_window(
                    window, user_id
                )
                
                # 保存为pending
                if extracted:
                    self.extraction_service.save_as_pending(
                        extracted, user_id, window
                    )
                
                processed += len(window)
        
        # 4. 标记已处理
        chunk_ids = [m.chunk_id for m in recent_messages]
        self.db.execute(
            """UPDATE chunks 
               SET processed_for_entities = true 
               WHERE chunk_id = ANY(%s)""",
            chunk_ids
        )
        
        return processed
```

### 3.2 人工审核API

```python
# services/agent/app/routers/entity_review.py

from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel

router = APIRouter(prefix="/api/entities", tags=["entities"])


class EntityApprovalRequest(BaseModel):
    canonical_name: Optional[str] = None
    aliases: Optional[List[str]] = None


class EntityBatchApprovalRequest(BaseModel):
    entity_ids: List[str]


class EntityMergeRequest(BaseModel):
    target_entity_id: str


class EntityReviewService:
    """
    实体审核服务
    """
    
    def __init__(self, db):
        self.db = db
    
    def list_pending(
        self, 
        user_id: str, 
        limit: int = 50,
        entity_type: Optional[str] = None
    ):
        """
        获取待审核实体列表
        """
        query = """
            SELECT 
                e.entity_id,
                e.canonical_name,
                e.entity_type,
                e.extraction_info,
                e.created_at,
                COUNT(cm.chunk_id) as mention_count,
                array_agg(
                    c.content ORDER BY c.sent_at DESC
                ) FILTER (WHERE c.content IS NOT NULL) as sample_contexts
            FROM entities e
            LEFT JOIN chunk_mounts cm ON e.entity_id = cm.entity_id
            LEFT JOIN chunks c ON cm.chunk_id = c.chunk_id
            WHERE e.status = 'pending'
              AND e.owner_user_id = %s
        """
        
        params = [user_id]
        
        if entity_type:
            query += " AND e.entity_type = %s"
            params.append(entity_type)
        
        query += """
            GROUP BY e.entity_id
            ORDER BY COUNT(cm.chunk_id) DESC, e.created_at DESC
            LIMIT %s
        """
        params.append(limit)
        
        return self.db.query(query, *params)
    
    def approve(
        self, 
        entity_id: str, 
        user_id: str,
        canonical_name: Optional[str] = None,
        aliases: Optional[List[str]] = None
    ):
        """
        批准实体（可修改名称和别名）
        """
        # 1. 检查权限
        entity = self._get_entity_with_permission(entity_id, user_id)
        
        if entity.status != 'pending':
            raise ValueError(f"Entity is not pending: {entity.status}")
        
        # 2. 更新实体
        updates = {
            "status": "active",
            "approved_at": "NOW()",
            "approved_by": user_id
        }
        
        if canonical_name:
            updates["canonical_name"] = canonical_name
        
        if aliases:
            updates["aliases"] = aliases
        
        set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
        values = list(updates.values()) + [entity_id]
        
        self.db.execute(
            f"""UPDATE entities 
                SET {set_clause}
                WHERE entity_id = %s""",
            *values
        )
        
        # 3. 【扩展性】触发自动合并建议（未来启用）
        # self._suggest_merge_candidates(entity_id)
        
        return {"success": True, "entity_id": entity_id}
    
    def reject(
        self, 
        entity_id: str, 
        user_id: str,
        reason: Optional[str] = None
    ):
        """
        拒绝实体
        """
        entity = self._get_entity_with_permission(entity_id, user_id)
        
        # 1. 更新状态
        self.db.execute(
            """UPDATE entities 
               SET status = 'rejected',
                   reject_reason = %s,
                   rejected_at = NOW()
               WHERE entity_id = %s""",
            reason, entity_id
        )
        
        # 2. 删除挂载关系
        self.db.execute(
            "DELETE FROM chunk_mounts WHERE entity_id = %s",
            entity_id
        )
        
        return {"success": True}
    
    def batch_approve(
        self, 
        entity_ids: List[str], 
        user_id: str
    ):
        """
        批量批准（不修改名称）
        """
        # 检查权限
        for entity_id in entity_ids:
            self._get_entity_with_permission(entity_id, user_id)
        
        self.db.execute(
            """UPDATE entities 
               SET status = 'active',
                   approved_at = NOW(),
                   approved_by = %s
               WHERE entity_id = ANY(%s)""",
            user_id, entity_ids
        )
        
        return {"success": True, "count": len(entity_ids)}


# API路由
@router.get("/pending")
def list_pending_entities(
    entity_type: Optional[str] = None,
    limit: int = 50,
    current_user = Depends(get_current_user),
    service: EntityReviewService = Depends(get_entity_review_service)
):
    """
    获取待审核实体列表
    """
    return service.list_pending(
        user_id=current_user.user_id,
        limit=limit,
        entity_type=entity_type
    )


@router.post("/{entity_id}/approve")
def approve_entity(
    entity_id: str,
    request: EntityApprovalRequest,
    current_user = Depends(get_current_user),
    service: EntityReviewService = Depends(get_entity_review_service)
):
    """
    批准实体
    """
    return service.approve(
        entity_id=entity_id,
        user_id=current_user.user_id,
        canonical_name=request.canonical_name,
        aliases=request.aliases
    )


@router.post("/{entity_id}/reject")
def reject_entity(
    entity_id: str,
    reason: Optional[str] = None,
    current_user = Depends(get_current_user),
    service: EntityReviewService = Depends(get_entity_review_service)
):
    """
    拒绝实体
    """
    return service.reject(
        entity_id=entity_id,
        user_id=current_user.user_id,
        reason=reason
    )


@router.post("/batch-approve")
def batch_approve_entities(
    request: EntityBatchApprovalRequest,
    current_user = Depends(get_current_user),
    service: EntityReviewService = Depends(get_entity_review_service)
):
    """
    批量批准实体
    """
    return service.batch_approve(
        entity_ids=request.entity_ids,
        user_id=current_user.user_id
    )
```

### 3.3 实体管理API

```python
# services/agent/app/routers/entity_management.py

from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api/entities", tags=["entities"])


class EntityUpdateRequest(BaseModel):
    canonical_name: Optional[str] = None
    aliases: Optional[List[str]] = None
    attributes: Optional[dict] = None


class EntityManagementService:
    """
    实体管理服务
    """
    
    def list_active(
        self, 
        user_id: str,
        entity_type: Optional[str] = None,
        limit: int = 100
    ):
        """
        获取已激活的实体
        """
        query = """
            SELECT e.*,
                   COUNT(cm.chunk_id) as chunk_count,
                   MAX(c.sent_at) as last_mentioned_at
            FROM entities e
            LEFT JOIN chunk_mounts cm ON e.entity_id = cm.entity_id
            LEFT JOIN chunks c ON cm.chunk_id = c.chunk_id
            WHERE e.status = 'active'
              AND e.owner_user_id = %s
        """
        
        params = [user_id]
        
        if entity_type:
            query += " AND e.entity_type = %s"
            params.append(entity_type)
        
        query += """
            GROUP BY e.entity_id
            ORDER BY COUNT(cm.chunk_id) DESC
            LIMIT %s
        """
        params.append(limit)
        
        return self.db.query(query, *params)
    
    def update(
        self, 
        entity_id: str, 
        user_id: str,
        canonical_name: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        attributes: Optional[dict] = None
    ):
        """
        更新实体信息
        """
        entity = self._get_entity_with_permission(entity_id, user_id)
        
        updates = {}
        if canonical_name:
            updates["canonical_name"] = canonical_name
        
        if aliases is not None:
            updates["aliases"] = aliases
        
        if attributes is not None:
            updates["attributes"] = json.dumps(attributes)
        
        if updates:
            set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
            values = list(updates.values()) + [entity_id]
            
            self.db.execute(
                f"UPDATE entities SET {set_clause} WHERE entity_id = %s",
                *values
            )
        
        return {"success": True}
    
    def merge(
        self, 
        source_id: str, 
        target_id: str, 
        user_id: str
    ):
        """
        人工合并实体
        """
        # 1. 检查权限
        source = self._get_entity_with_permission(source_id, user_id)
        target = self._get_entity_with_permission(target_id, user_id)
        
        # 2. 记录合并历史（用于回滚）
        self.db.execute(
            """INSERT INTO entity_merge_history (
                source_entity_id,
                target_entity_id,
                source_snapshot,
                target_snapshot_before,
                merge_type,
                merged_by
            ) VALUES (%s, %s, %s, %s, %s, %s)""",
            source_id,
            target_id,
            json.dumps(source.__dict__),
            json.dumps(target.__dict__),
            'manual',
            user_id
        )
        
        # 3. 转移所有挂载
        self.db.execute(
            """UPDATE chunk_mounts 
               SET entity_id = %s 
               WHERE entity_id = %s""",
            target_id, source_id
        )
        
        # 4. 合并别名
        merged_aliases = list(set(
            target.aliases + source.aliases + [source.canonical_name]
        ))
        
        self.db.execute(
            """UPDATE entities 
               SET aliases = %s 
               WHERE entity_id = %s""",
            merged_aliases, target_id
        )
        
        # 5. 标记source为merged
        self.db.execute(
            """UPDATE entities 
               SET status = 'merged',
                   merged_into = %s
               WHERE entity_id = %s""",
            target_id, source_id
        )
        
        return {
            "success": True, 
            "target_entity_id": target_id
        }
    
    def search(
        self, 
        query: str, 
        user_id: str,
        entity_type: Optional[str] = None,
        limit: int = 20
    ):
        """
        搜索实体（用于合并时查找目标）
        """
        sql = """
            SELECT e.*,
                   COUNT(cm.chunk_id) as chunk_count,
                   similarity(e.canonical_name, %s) as sim_score
            FROM entities e
            LEFT JOIN chunk_mounts cm ON e.entity_id = cm.entity_id
            WHERE e.status = 'active'
              AND e.owner_user_id = %s
              AND (
                  e.canonical_name ILIKE %s
                  OR e.canonical_name %% %s
                  OR %s = ANY(e.aliases)
              )
        """
        
        params = [query, user_id, f"%{query}%", query, query]
        
        if entity_type:
            sql += " AND e.entity_type = %s"
            params.append(entity_type)
        
        sql += """
            GROUP BY e.entity_id
            ORDER BY sim_score DESC, COUNT(cm.chunk_id) DESC
            LIMIT %s
        """
        params.append(limit)
        
        return self.db.query(sql, *params)


# API路由
@router.get("")
def list_entities(
    entity_type: Optional[str] = None,
    limit: int = 100,
    current_user = Depends(get_current_user),
    service: EntityManagementService = Depends(get_entity_management_service)
):
    """
    获取已激活的实体列表
    """
    return service.list_active(
        user_id=current_user.user_id,
        entity_type=entity_type,
        limit=limit
    )


@router.put("/{entity_id}")
def update_entity(
    entity_id: str,
    request: EntityUpdateRequest,
    current_user = Depends(get_current_user),
    service: EntityManagementService = Depends(get_entity_management_service)
):
    """
    更新实体信息
    """
    return service.update(
        entity_id=entity_id,
        user_id=current_user.user_id,
        canonical_name=request.canonical_name,
        aliases=request.aliases,
        attributes=request.attributes
    )


@router.post("/{source_id}/merge")
def merge_entities(
    source_id: str,
    request: EntityMergeRequest,
    current_user = Depends(get_current_user),
    service: EntityManagementService = Depends(get_entity_management_service)
):
    """
    合并实体
    """
    return service.merge(
        source_id=source_id,
        target_id=request.target_entity_id,
        user_id=current_user.user_id
    )


@router.get("/search")
def search_entities(
    q: str,
    entity_type: Optional[str] = None,
    limit: int = 20,
    current_user = Depends(get_current_user),
    service: EntityManagementService = Depends(get_entity_management_service)
):
    """
    搜索实体
    """
    return service.search(
        query=q,
        user_id=current_user.user_id,
        entity_type=entity_type,
        limit=limit
    )
```

## 4. 未来自动化扩展点

### 4.1 自动合并建议（Phase 2）

```python
# 【预留】未来启用的自动合并建议功能

class AutoMergeSuggestionService:
    """
    自动生成合并建议（不自动执行）
    """
    
    def suggest_merge_candidates(self, entity_id: str):
        """
        为新激活的实体生成合并建议
        """
        entity = self.db.get_entity(entity_id)
        
        # 1. 查找候选实体
        candidates = self._find_merge_candidates(entity)
        
        # 2. LLM判断相似度
        suggestions = []
        for candidate in candidates:
            decision = self._llm_should_merge(entity, candidate)
            
            if decision.confidence > 0.7:
                suggestions.append({
                    "target_entity_id": candidate.entity_id,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "evidence_chunks": decision.evidence_chunks,
                    "suggested_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending"
                })
        
        # 3. 保存建议到entity.merge_suggestions
        if suggestions:
            self.db.execute(
                """UPDATE entities 
                   SET merge_suggestions = %s 
                   WHERE entity_id = %s""",
                json.dumps(suggestions), entity_id
            )
        
        return suggestions
    
    def _find_merge_candidates(self, entity):
        """
        查找可能重复的实体
        """
        # 策略1：姓氏匹配（人名）
        if entity.entity_type == 'person':
            candidates = self._find_by_surname(entity)
        
        # 策略2：名称相似度
        candidates.extend(self._find_by_similarity(entity))
        
        # 策略3：共现关系
        candidates.extend(self._find_by_co_occurrence(entity))
        
        return dedupe_candidates(candidates)


# API：获取合并建议
@router.get("/{entity_id}/merge-suggestions")
def get_merge_suggestions(
    entity_id: str,
    current_user = Depends(get_current_user),
    service: AutoMergeSuggestionService = Depends(...)
):
    """
    获取自动生成的合并建议
    """
    entity = service.get_entity(entity_id, current_user.user_id)
    
    suggestions = entity.merge_suggestions or []
    
    # 只返回pending状态的建议
    pending = [s for s in suggestions if s.get("status") == "pending"]
    
    return {
        "entity_id": entity_id,
        "suggestions": pending
    }


# API：接受/拒绝建议
@router.post("/{entity_id}/merge-suggestions/{suggestion_index}/accept")
def accept_merge_suggestion(
    entity_id: str,
    suggestion_index: int,
    current_user = Depends(get_current_user),
    merge_service: EntityManagementService = Depends(...),
    suggestion_service: AutoMergeSuggestionService = Depends(...)
):
    """
    接受合并建议
    """
    # 1. 获取建议
    suggestion = suggestion_service.get_suggestion(entity_id, suggestion_index)
    
    # 2. 执行合并
    result = merge_service.merge(
        source_id=entity_id,
        target_id=suggestion["target_entity_id"],
        user_id=current_user.user_id
    )
    
    # 3. 标记建议为accepted
    suggestion_service.mark_suggestion_status(
        entity_id, suggestion_index, "accepted"
    )
    
    return result
```

### 4.2 实体消歧（Phase 3）

```python
# 【预留】未来的实体消歧功能

class EntityDisambiguationService:
    """
    处理不确定实体（如"小张"）
    """
    
    def create_uncertain_entity(
        self, 
        mention: str, 
        context_clues: dict,
        user_id: str
    ):
        """
        创建不确定状态的实体
        """
        return self.db.insert(
            """INSERT INTO entities (
                owner_user_id,
                canonical_name,
                entity_type,
                status,
                attributes
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING entity_id""",
            user_id,
            mention,  # 暂时用昵称
            'person',
            'uncertain',  # 新状态
            json.dumps(context_clues)
        )
    
    def resolve_uncertain_entities(self, user_id: str):
        """
        定期任务：尝试解析不确定实体
        """
        uncertain = self.db.query(
            """SELECT e.*, COUNT(cm.chunk_id) as mention_count
               FROM entities e
               JOIN chunk_mounts cm ON e.entity_id = cm.entity_id
               WHERE e.status = 'uncertain'
                 AND e.owner_user_id = %s
               GROUP BY e.entity_id
               HAVING COUNT(cm.chunk_id) >= 5""",
            user_id
        )
        
        for entity in uncertain:
            # 收集更多上下文
            contexts = self._get_all_contexts(entity.entity_id)
            
            # LLM重新判断
            candidates = self._find_disambiguation_candidates(entity)
            
            if candidates:
                decision = self._llm_disambiguate(entity, candidates, contexts)
                
                if decision.should_merge and decision.confidence > 0.8:
                    # 高置信度自动合并
                    self.merge(entity.entity_id, decision.target_id)
                elif decision.confidence > 0.6:
                    # 中等置信度：生成建议，人工确认
                    self._create_merge_suggestion(entity, decision)


# 数据库增加uncertain状态
ALTER TABLE entities DROP CONSTRAINT status_chk;
ALTER TABLE entities ADD CONSTRAINT status_chk CHECK (status IN (
    'pending', 'active', 'rejected', 'merged', 'uncertain'
));
```

### 4.3 批量操作与模式学习（Phase 4）

```python
# 【预留】学习人工审核模式

class EntityReviewPatternLearner:
    """
    学习人工审核的模式，用于提高自动化准确率
    """
    
    def analyze_approval_patterns(self, user_id: str):
        """
        分析用户的批准模式
        """
        # 获取过去30天的审核记录
        approved = self.db.query(
            """SELECT e.*, e.extraction_info
               FROM entities e
               WHERE e.owner_user_id = %s
                 AND e.status = 'active'
                 AND e.approved_at > NOW() - INTERVAL '30 days'""",
            user_id
        )
        
        # 分析模式
        patterns = {
            "auto_approve_threshold": self._compute_confidence_threshold(approved),
            "common_name_corrections": self._find_name_corrections(approved),
            "preferred_alias_patterns": self._find_alias_patterns(approved)
        }
        
        return patterns
    
    def suggest_auto_approval_rules(self, user_id: str):
        """
        建议自动批准规则
        """
        patterns = self.analyze_approval_patterns(user_id)
        
        rules = []
        
        # 规则1：高置信度自动批准
        if patterns["auto_approve_threshold"] > 0.85:
            rules.append({
                "type": "confidence_threshold",
                "threshold": patterns["auto_approve_threshold"],
                "description": f"LLM置信度 > {patterns['auto_approve_threshold']}时自动批准"
            })
        
        # 规则2：特定类型自动批准
        # ...
        
        return rules
```

## 5. 前端界面（简化版）

### 5.1 实体审核页面

```vue
<!-- EntityReviewPage.vue -->
<template>
  <div class="entity-review-page">
    <h1>实体审核</h1>
    <div class="stats">
      <span>待审核: {{ pendingCount }}个</span>
      <button @click="batchApprove" :disabled="selectedIds.length === 0">
        批量批准 ({{ selectedIds.length }})
      </button>
    </div>
    
    <div class="filter-tabs">
      <button 
        v-for="type in entityTypes" 
        :key="type.value"
        :class="{ active: filterType === type.value }"
        @click="filterType = type.value"
      >
        {{ type.label }}
      </button>
    </div>
    
    <div class="entity-list">
      <EntityReviewCard
        v-for="entity in filteredEntities"
        :key="entity.entity_id"
        :entity="entity"
        :selected="selectedIds.includes(entity.entity_id)"
        @select="toggleSelect"
        @approve="handleApprove"
        @reject="handleReject"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';
import { api } from '@/api';

const pendingEntities = ref([]);
const selectedIds = ref([]);
const filterType = ref(null);

const entityTypes = [
  { value: null, label: '全部' },
  { value: 'organization', label: '公司' },
  { value: 'person', label: '人员' },
  { value: 'project', label: '项目' },
  { value: 'policy', label: '政策' },
  { value: 'contract', label: '合同' }
];

const filteredEntities = computed(() => {
  if (!filterType.value) return pendingEntities.value;
  return pendingEntities.value.filter(e => e.entity_type === filterType.value);
});

const pendingCount = computed(() => pendingEntities.value.length);

async function loadPendingEntities() {
  const response = await api.get('/entities/pending', {
    params: { entity_type: filterType.value }
  });
  pendingEntities.value = response.data.entities;
}

function toggleSelect(entityId) {
  const index = selectedIds.value.indexOf(entityId);
  if (index > -1) {
    selectedIds.value.splice(index, 1);
  } else {
    selectedIds.value.push(entityId);
  }
}

async function handleApprove(entityId, canonicalName, aliases) {
  await api.post(`/entities/${entityId}/approve`, {
    canonical_name: canonicalName,
    aliases: aliases
  });
  
  pendingEntities.value = pendingEntities.value.filter(
    e => e.entity_id !== entityId
  );
}

async function handleReject(entityId) {
  if (!confirm('确认拒绝该实体？')) return;
  
  await api.post(`/entities/${entityId}/reject`);
  
  pendingEntities.value = pendingEntities.value.filter(
    e => e.entity_id !== entityId
  );
}

async function batchApprove() {
  if (!confirm(`确认批准${selectedIds.value.length}个实体？`)) return;
  
  await api.post('/entities/batch-approve', {
    entity_ids: selectedIds.value
  });
  
  pendingEntities.value = pendingEntities.value.filter(
    e => !selectedIds.value.includes(e.entity_id)
  );
  selectedIds.value = [];
}

onMounted(() => {
  loadPendingEntities();
});
</script>
```

### 5.2 实体审核卡片

```vue
<!-- EntityReviewCard.vue -->
<template>
  <div class="entity-card" :class="{ selected }">
    <div class="card-header">
      <input 
        type="checkbox" 
        :checked="selected"
        @change="$emit('select', entity.entity_id)"
      />
      <span class="entity-type">{{ entityTypeLabel }}</span>
      <span class="mention-count">提及 {{ entity.mention_count }} 次</span>
    </div>
    
    <div class="card-body">
      <!-- 名称编辑 -->
      <div class="field">
        <label>规范名称：</label>
        <input 
          v-model="editedName"
          placeholder="修改为规范的名称"
          class="name-input"
        />
      </div>
      
      <!-- 别名管理 -->
      <div class="field">
        <label>别名/提法：</label>
        <div class="tag-list">
          <span 
            v-for="(alias, i) in aliases" 
            :key="i"
            class="tag"
          >
            {{ alias }}
            <button @click="removeAlias(i)">×</button>
          </span>
          <input 
            v-model="newAlias"
            @keypress.enter="addAlias"
            placeholder="添加别名"
            class="alias-input"
          />
        </div>
      </div>
      
      <!-- LLM提取信息 -->
      <div class="field" v-if="extractionInfo">
        <label>提取依据：</label>
        <p class="evidence">{{ extractionInfo.evidence }}</p>
        <span class="confidence">
          置信度: {{ (extractionInfo.confidence * 100).toFixed(0) }}%
        </span>
      </div>
      
      <!-- 上下文样例 -->
      <div class="field">
        <label>出现上下文：</label>
        <div class="contexts">
          <p 
            v-for="(ctx, i) in entity.sample_contexts?.slice(0, 3)" 
            :key="i"
            class="context-sample"
          >
            {{ ctx }}
          </p>
        </div>
      </div>
    </div>
    
    <div class="card-footer">
      <button 
        @click="handleApprove"
        class="btn-approve"
      >
        批准
      </button>
      <button 
        @click="handleReject"
        class="btn-reject"
      >
        拒绝
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue';

const props = defineProps({
  entity: Object,
  selected: Boolean
});

const emit = defineEmits(['select', 'approve', 'reject']);

const editedName = ref(props.entity.canonical_name);
const aliases = ref([...(props.entity.extraction_info?.mentions || [])]);
const newAlias = ref('');

const extractionInfo = computed(() => props.entity.extraction_info);

const entityTypeLabel = computed(() => {
  const labels = {
    organization: '公司',
    person: '人员',
    project: '项目',
    policy: '政策',
    contract: '合同'
  };
  return labels[props.entity.entity_type] || props.entity.entity_type;
});

function addAlias() {
  if (newAlias.value.trim()) {
    aliases.value.push(newAlias.value.trim());
    newAlias.value = '';
  }
}

function removeAlias(index) {
  aliases.value.splice(index, 1);
}

function handleApprove() {
  emit('approve', props.entity.entity_id, editedName.value, aliases.value);
}

function handleReject() {
  emit('reject', props.entity.entity_id);
}
</script>

<style scoped>
.entity-card {
  border: 1px solid #e0e0e0;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
  background: white;
}

.entity-card.selected {
  border-color: #1890ff;
  background: #f0f8ff;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.entity-type {
  display: inline-block;
  padding: 4px 12px;
  background: #f0f0f0;
  border-radius: 4px;
  font-size: 12px;
}

.field {
  margin-bottom: 12px;
}

.field label {
  display: block;
  font-weight: 500;
  margin-bottom: 4px;
  font-size: 14px;
}

.name-input {
  width: 100%;
  padding: 8px;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: #e8f4ff;
  border-radius: 4px;
  font-size: 13px;
}

.tag button {
  border: none;
  background: none;
  cursor: pointer;
  color: #999;
}

.evidence {
  color: #666;
  font-size: 14px;
  line-height: 1.5;
}

.confidence {
  display: inline-block;
  margin-top: 4px;
  padding: 2px 8px;
  background: #f0f0f0;
  border-radius: 4px;
  font-size: 12px;
}

.contexts {
  max-height: 200px;
  overflow-y: auto;
}

.context-sample {
  padding: 8px;
  background: #f9f9f9;
  border-left: 3px solid #1890ff;
  margin-bottom: 8px;
  font-size: 13px;
  color: #555;
}

.card-footer {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.btn-approve {
  flex: 1;
  padding: 10px;
  background: #52c41a;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-weight: 500;
}

.btn-reject {
  flex: 1;
  padding: 10px;
  background: #f5f5f5;
  color: #333;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
  cursor: pointer;
}
</style>
```

## 6. 成本估算

```yaml
MVP阶段成本:

开发成本:
  - 后端开发: 1周（LLM提取 + API）
  - 前端开发: 1周（审核界面 + 管理界面）
  - 测试联调: 2-3天
  - 总计: 2-3周

运行成本（每月）:
  - LLM调用: $10-20
    - 每小时1000条消息 → 40个窗口需要LLM
    - 40 × 24 × 30 = 28,800次/月
    - 28,800 × 2500 tokens × $0.15/1M ≈ $10.8
  
  - 人工审核: 初期每天30分钟
    - 随着实体库增长，审核量下降
    - 3个月后可能每天10分钟

数据库存储:
  - 实体表: 10万实体 × 2KB ≈ 200MB
  - 挂载表: 3000万条 × 100B ≈ 3GB
  - 历史表: 1万次合并 × 10KB ≈ 100MB
  - 总计: ~3.5GB（可忽略）
```

## 7. 实施计划

### Week 1-2: 核心开发

```yaml
后端:
  - [ ] entities表迁移
  - [ ] entity_merge_history表
  - [ ] LLM提取服务
  - [ ] 定时扫描worker
  - [ ] 审核API
  - [ ] 管理API

前端:
  - [ ] 实体审核页面
  - [ ] 实体管理页面
  - [ ] 合并对话框

测试:
  - [ ] 单元测试
  - [ ] 集成测试
  - [ ] 人工测试
```

### Week 3: 灰度上线

```yaml
灰度策略:
  - 先在1-2个测试用户上启用
  - 观察提取质量和审核效率
  - 收集反馈，快速迭代

监控指标:
  - LLM提取准确率
  - 人工批准率
  - 人工拒绝率
  - 平均审核时间
```

### Month 2-3: 优化迭代

```yaml
优化方向:
  - 提示词优化（提升提取准确率）
  - 界面优化（提升审核效率）
  - 批量操作（减少重复劳动）
  - 数据分析（为自动化做准备）
```

### Month 4+: 自动化扩展

```yaml
Phase 2: 自动合并建议
  - 启用merge_suggestions功能
  - 人工审核建议，逐步信任

Phase 3: 部分自动批准
  - 高置信度自动批准（>0.9）
  - 学习人工审核模式

Phase 4: 智能消歧
  - 处理不确定实体
  - 自动合并高置信度重复
```

## 8. 风险与对策

### 8.1 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| LLM提取不准确 | 高 | 人工审核兜底 + 提示词优化 |
| LLM成本超预算 | 中 | 缓存 + 批量处理 + 限流 |
| 人工审核负担重 | 中 | 批量操作 + 高频实体优先 |

### 8.2 产品风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 用户不愿意审核 | 高 | 强调价值 + 简化流程 + 游戏化 |
| 实体重复严重 | 中 | 合并功能 + 搜索辅助 |
| 冷启动期检索效果差 | 低 | 传统RAG降级 + 快速积累种子实体 |

## 9. 成功指标

```yaml
MVP验收标准:

功能完整性:
  ✅ LLM能提取实体并保存为pending
  ✅ 人工能审核、修改、批准/拒绝
  ✅ 人工能合并重复实体
  ✅ 树形RAG只使用active实体

质量指标:
  - LLM提取准确率 > 70%
  - 人工批准率 > 80%
  - 平均审核时间 < 30秒/实体

性能指标:
  - LLM调用延迟 < 2秒
  - API响应时间 < 500ms
  - 每小时处理 > 1000条消息

可扩展性:
  ✅ 数据模型支持自动合并建议
  ✅ 代码预留扩展点
  ✅ 记录完整的操作历史
```

---

**文档版本控制**:
- v1.0 (2024-01-XX): MVP方案，人工审核为主，保留自动化扩展性
