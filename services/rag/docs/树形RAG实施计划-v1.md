# 树形RAG系统 - 分阶段实施计划

## 文档状态

- **版本**: v1.0
- **创建日期**: 2024-01-XX
- **负责人**: [待定]
- **目标**: 分阶段实施树形RAG系统，从MVP到完整功能的渐进式交付

## 1. 总体规划

### 1.1 实施原则

```yaml
原则1: 渐进式交付
  - 每个阶段都能独立上线
  - 每个阶段都有可验证的价值
  - 不阻塞现有RAG服务

原则2: 风险可控
  - 保留传统RAG作为降级方案
  - 每个阶段都有回滚机制
  - 灰度发布，小范围验证

原则3: 数据驱动
  - 每个阶段设定明确的成功指标
  - 根据数据决定是否进入下一阶段
  - 持续优化而非一次到位
```

### 1.2 整体时间线

```
┌─────────────────────────────────────────────────────────────────┐
│                        4-6个月完整实施                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Phase 0        Phase 1         Phase 2         Phase 3          │
│  基础准备       MVP上线         优化迭代        自动化升级       │
│  2周            3周             4周             持续              │
│  ────────>     ────────>       ────────>       ────────>         │
│  数据库         人工审核         性能优化        自动合并          │
│  LLM提取        树形检索         用户反馈        智能消歧          │
│  审核界面       灰度验证         指标监控        模式学习          │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 里程碑定义

| 里程碑 | 时间 | 交付物 | 验收标准 |
|--------|------|--------|----------|
| **M0: 基础准备完成** | Week 2 | 数据模型、LLM提取服务 | 能提取实体并保存 |
| **M1: MVP上线** | Week 5 | 审核界面、树形检索 | 1个用户完整流程跑通 |
| **M2: 生产可用** | Week 9 | 性能优化、监控告警 | 10个用户稳定运行 |
| **M3: 规模推广** | Week 13+ | 自动化功能 | 全量用户上线 |

---

## 2. Phase 0: 基础准备（Week 1-2）

### 2.1 目标

```
建立树形RAG的基础设施，但不影响现有系统运行
```

### 2.2 交付物

#### 2.2.1 数据库Schema

```sql
-- 在RAG数据库中新增表
-- services/rag/db/migrations/000010_tree_rag_entities.up.sql

-- 实体表
CREATE TABLE entities (
    entity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id TEXT NOT NULL,
    organization_id TEXT,
    canonical_name VARCHAR(200) NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    attributes JSONB DEFAULT '{}',
    extraction_info JSONB DEFAULT '{}',
    status VARCHAR(32) DEFAULT 'pending',
    merge_suggestions JSONB DEFAULT '[]',
    reject_reason TEXT,
    merged_into UUID REFERENCES entities(entity_id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    approved_by TEXT,
    rejected_at TIMESTAMPTZ,
    chunk_count INT DEFAULT 0,
    last_mentioned_at TIMESTAMPTZ,
    CONSTRAINT entity_type_chk CHECK (entity_type IN (
        'organization', 'person', 'project', 'policy', 'contract'
    )),
    CONSTRAINT status_chk CHECK (status IN (
        'pending', 'active', 'rejected', 'merged'
    ))
);

-- Chunk挂载表（多对多）
CREATE TABLE chunk_mounts (
    mount_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID NOT NULL,
    entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    confidence FLOAT NOT NULL DEFAULT 0.8,
    mount_method VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(chunk_id, entity_id)
);

-- 实体关系表
CREATE TABLE entity_relations (
    relation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    target_entity_id UUID REFERENCES entities(entity_id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL,
    confidence FLOAT DEFAULT 0.8,
    evidence_chunk_ids UUID[] DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_entity_id, target_entity_id, relation_type)
);

-- 实体合并历史
CREATE TABLE entity_merge_history (
    merge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id UUID NOT NULL,
    target_entity_id UUID NOT NULL,
    source_snapshot JSONB NOT NULL,
    target_snapshot_before JSONB NOT NULL,
    merge_type VARCHAR(32) NOT NULL,
    confidence FLOAT,
    reason TEXT,
    merged_by TEXT NOT NULL,
    merged_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT merge_type_chk CHECK (merge_type IN (
        'manual', 'auto_suggested', 'auto_confirmed'
    ))
);

-- 索引
CREATE INDEX entities_owner_status_idx ON entities(owner_user_id, status);
CREATE INDEX entities_status_idx ON entities(status) WHERE status = 'pending';
CREATE INDEX entities_type_idx ON entities(entity_type);
CREATE INDEX entities_name_trgm_idx ON entities USING gin(canonical_name gin_trgm_ops);
CREATE INDEX chunk_mounts_chunk_idx ON chunk_mounts(chunk_id);
CREATE INDEX chunk_mounts_entity_idx ON chunk_mounts(entity_id, confidence);
CREATE INDEX entity_relations_source_idx ON entity_relations(source_entity_id, relation_type);
CREATE INDEX entity_relations_target_idx ON entity_relations(target_entity_id, relation_type);

-- Chunks表增加字段
ALTER TABLE chunks ADD COLUMN processed_for_entities BOOLEAN DEFAULT FALSE;
CREATE INDEX chunks_processed_entities_idx ON chunks(processed_for_entities) 
    WHERE processed_for_entities = FALSE;
```

#### 2.2.2 LLM实体提取服务

```python
# services/rag/app/application/entity_extraction_service.py

class EntityExtractionService:
    """
    从消息窗口中提取实体
    """
    def extract_from_window(
        self, 
        window: List[Message], 
        user_id: str
    ) -> List[ExtractedEntity]:
        # 实现LLM提取逻辑
        pass
    
    def save_as_pending(
        self, 
        extracted: List[ExtractedEntity], 
        user_id: str,
        window: List[Message]
    ):
        # 保存为pending状态
        pass
```

#### 2.2.3 定时提取Worker

```python
# services/rag/worker.py 扩展

class EntityExtractionWorker:
    """
    定时扫描消息，提取实体
    """
    def run_once(self, batch_size: int = 100):
        # 获取未处理的消息
        # 滑动窗口
        # 调用LLM提取
        # 保存结果
        pass
```

### 2.3 任务清单

#### Week 1: 数据库和基础服务

- [ ] **Day 1-2**: 数据库Schema设计和Review
  - [ ] 编写迁移脚本
  - [ ] 本地测试迁移
  - [ ] Code Review

- [ ] **Day 3-4**: LLM提取服务开发
  - [ ] Prompt设计和测试
  - [ ] EntityExtractionService实现
  - [ ] 单元测试（Mock LLM）

- [ ] **Day 5**: 定时Worker开发
  - [ ] EntityExtractionWorker实现
  - [ ] 滑动窗口逻辑
  - [ ] 集成测试

#### Week 2: 测试和验证

- [ ] **Day 1-2**: 端到端测试
  - [ ] 准备测试数据（50条真实消息）
  - [ ] 运行提取流程
  - [ ] 验证提取质量

- [ ] **Day 3-4**: 性能测试
  - [ ] LLM调用延迟测试
  - [ ] 数据库写入性能测试
  - [ ] Worker吞吐量测试

- [ ] **Day 5**: 部署到测试环境
  - [ ] 数据库迁移（测试环境）
  - [ ] 部署新版本服务
  - [ ] 监控观察

### 2.4 验收标准

```yaml
功能验收:
  ✅ 数据库迁移成功执行
  ✅ LLM能从窗口提取实体
  ✅ 实体保存为pending状态
  ✅ Chunks正确挂载到实体
  ✅ Worker能定时运行

质量验收:
  ✅ LLM提取准确率 > 70%（人工抽样50条）
  ✅ 单元测试覆盖率 > 80%
  ✅ 无明显性能问题

文档验收:
  ✅ API文档更新
  ✅ 数据库Schema文档
  ✅ 部署文档
```

### 2.5 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 数据库迁移失败 | 低 | 高 | 在测试环境充分测试，准备回滚脚本 |
| LLM提取质量差 | 中 | 中 | 迭代Prompt，准备多个版本 |
| Worker影响现有服务 | 低 | 高 | 资源隔离，限流保护 |

---

## 3. Phase 1: MVP上线（Week 3-5）

### 3.1 目标

```
实现完整的人工审核流程，树形检索在灰度用户上生效
```

### 3.2 交付物

#### 3.2.1 后端API

```python
# services/rag/app/routers/entity_review.py

@router.get("/api/entities/pending")
def list_pending_entities():
    """获取待审核实体列表"""
    pass

@router.post("/api/entities/{entity_id}/approve")
def approve_entity(entity_id, request: EntityApprovalRequest):
    """批准实体（可修改名称和别名）"""
    pass

@router.post("/api/entities/{entity_id}/reject")
def reject_entity(entity_id, reason: str):
    """拒绝实体"""
    pass

@router.post("/api/entities/batch-approve")
def batch_approve_entities(request: EntityBatchApprovalRequest):
    """批量批准"""
    pass


# services/rag/app/routers/entity_management.py

@router.get("/api/entities")
def list_entities(entity_type: str = None):
    """获取已激活实体列表"""
    pass

@router.put("/api/entities/{entity_id}")
def update_entity(entity_id, request: EntityUpdateRequest):
    """更新实体信息"""
    pass

@router.post("/api/entities/{source_id}/merge")
def merge_entities(source_id, request: EntityMergeRequest):
    """合并实体"""
    pass

@router.get("/api/entities/search")
def search_entities(q: str, entity_type: str = None):
    """搜索实体（用于合并）"""
    pass
```

#### 3.2.2 前端审核界面

```
apps/web/src/views/entity/
├─ EntityReviewPage.vue       # 审核主页面
├─ EntityReviewCard.vue       # 实体审核卡片
├─ EntityManagementPage.vue   # 实体管理页面
├─ EntityMergeDialog.vue      # 合并对话框
└─ EntityEditDialog.vue       # 编辑对话框
```

#### 3.2.3 树形检索服务

```python
# services/rag/app/application/tree_rag_service.py

class TreeRAGService:
    """
    树形RAG检索服务
    """
    def search(
        self, 
        query: str, 
        user_id: str,
        conversation_id: str = None,
        tree_mode: str = "shadow"  # off | shadow | boost
    ):
        # 实体定位
        # 树检索
        # 降级逻辑
        pass
```

### 3.3 任务清单

#### Week 3: 后端开发

- [ ] **Day 1-2**: 审核API开发
  - [ ] EntityReviewService实现
  - [ ] API路由实现
  - [ ] 单元测试

- [ ] **Day 3-4**: 管理API开发
  - [ ] EntityManagementService实现
  - [ ] 合并逻辑实现
  - [ ] 单元测试

- [ ] **Day 5**: 树形检索服务开发
  - [ ] TreeRAGService实现
  - [ ] 集成现有RAGService
  - [ ] 降级逻辑

#### Week 4: 前端开发

- [ ] **Day 1-2**: 审核页面开发
  - [ ] EntityReviewPage组件
  - [ ] EntityReviewCard组件
  - [ ] API集成

- [ ] **Day 3-4**: 管理页面开发
  - [ ] EntityManagementPage组件
  - [ ] 合并和编辑对话框
  - [ ] 搜索功能

- [ ] **Day 5**: 联调测试
  - [ ] 前后端联调
  - [ ] UI/UX优化
  - [ ] Bug修复

#### Week 5: 测试和灰度发布

- [ ] **Day 1-2**: 集成测试
  - [ ] 完整流程测试
  - [ ] 边界情况测试
  - [ ] 性能测试

- [ ] **Day 3**: 灰度发布准备
  - [ ] 配置灰度开关
  - [ ] 监控告警配置
  - [ ] 回滚预案

- [ ] **Day 4**: 灰度发布
  - [ ] 部署到生产环境
  - [ ] 开启1-2个测试用户
  - [ ] 实时监控

- [ ] **Day 5**: 观察和调整
  - [ ] 收集用户反馈
  - [ ] Bug修复
  - [ ] 指标分析

### 3.4 验收标准

```yaml
功能验收:
  ✅ 人工能审核pending实体
  ✅ 人工能修改实体名称和别名
  ✅ 人工能合并重复实体
  ✅ 树形检索能正确定位实体
  ✅ 树形检索失败时能降级

质量验收:
  ✅ 审核流程体验流畅（<30秒/实体）
  ✅ 树形检索准确率 > 传统RAG
  ✅ 树形检索延迟 < 200ms（p95）
  ✅ 无P0/P1 Bug

用户验收:
  ✅ 1-2个灰度用户完整流程跑通
  ✅ 用户满意度 >= 4/5
  ✅ 至少审核50个实体
```

### 3.5 灰度策略

```yaml
灰度阶段1（Day 1-3）:
  用户数: 1个内部测试用户
  tree_mode: shadow（只计算诊断，不影响结果）
  目标: 验证功能完整性

灰度阶段2（Day 4-7）:
  用户数: 2-3个早期用户
  tree_mode: shadow
  目标: 收集真实反馈

灰度阶段3（Week 6+）:
  用户数: 10个用户
  tree_mode: boost（影响排序）
  目标: 验证效果提升

全量（Phase 2后）:
  用户数: 全部
  tree_mode: boost（默认）
  目标: 规模化运行
```

### 3.6 回滚预案

```yaml
触发条件:
  - P0级Bug（数据丢失、服务不可用）
  - 用户强烈投诉
  - 性能严重下降

回滚步骤:
  1. 关闭树形检索（tree_mode=off）
  2. 停止实体提取Worker
  3. 前端隐藏审核入口
  4. 观察指标恢复
  
数据保留:
  - 不删除entities表数据
  - 保留用户已审核的实体
  - 可以随时重新开启
```

---

## 4. Phase 2: 优化迭代（Week 6-9）

### 4.1 目标

```
根据灰度反馈优化系统，提升性能和用户体验，扩大灰度范围
```

### 4.2 优化方向

#### 4.2.1 提取质量优化

```yaml
问题诊断:
  - 分析被拒绝的实体
  - 统计常见错误模式
  - 收集用户修正记录

优化措施:
  - Prompt迭代（提升准确率）
  - 增加实体类型白名单
  - 改进窗口大小和重叠策略
  - 添加后处理过滤器

目标:
  - LLM提取准确率 > 85%
  - 人工批准率 > 85%
```

#### 4.2.2 审核效率优化

```yaml
问题诊断:
  - 统计平均审核时间
  - 分析耗时操作
  - 收集用户操作习惯

优化措施:
  - 批量操作优化
  - 键盘快捷键
  - 智能推荐默认名称
  - 历史记录快速填充

目标:
  - 平均审核时间 < 20秒/实体
  - 批量操作支持 > 50个
```

#### 4.2.3 检索性能优化

```yaml
问题诊断:
  - 慢查询分析
  - 索引使用情况
  - 缓存命中率

优化措施:
  - 实体定位缓存（Redis）
  - 数据库索引优化
  - 批量查询优化
  - 连接池调优

目标:
  - 实体定位延迟 < 50ms（p95）
  - 树形检索延迟 < 150ms（p95）
  - 缓存命中率 > 80%
```

#### 4.2.4 监控和告警

```yaml
关键指标:
  业务指标:
    - pending实体数量
    - 每日新增实体
    - 人工批准率
    - 人工拒绝率
    - 树形检索成功率
  
  性能指标:
    - LLM调用延迟（p50, p95, p99）
    - 实体定位延迟
    - 树形检索延迟
    - 数据库查询延迟
  
  成本指标:
    - LLM调用次数
    - LLM token消耗
    - 每日LLM成本

告警规则:
  - pending实体积压 > 100个
  - LLM调用失败率 > 5%
  - 树形检索失败率 > 10%
  - 检索延迟 p95 > 500ms
  - 每日LLM成本 > $2
```

### 4.3 任务清单

#### Week 6: 数据分析和问题诊断

- [ ] **Day 1-2**: 数据分析
  - [ ] 统计提取准确率
  - [ ] 分析拒绝原因
  - [ ] 用户行为分析

- [ ] **Day 3-4**: 性能分析
  - [ ] 慢查询分析
  - [ ] 瓶颈识别
  - [ ] 制定优化方案

- [ ] **Day 5**: 用户访谈
  - [ ] 收集用户反馈
  - [ ] 识别痛点
  - [ ] 确定优先级

#### Week 7: 质量优化

- [ ] **Day 1-2**: Prompt优化
  - [ ] 设计新版本Prompt
  - [ ] A/B测试对比
  - [ ] 上线优化版本

- [ ] **Day 3-4**: 过滤器优化
  - [ ] 实现后处理规则
  - [ ] 白名单/黑名单
  - [ ] 测试验证

- [ ] **Day 5**: 效果评估
  - [ ] 统计准确率提升
  - [ ] 对比优化前后

#### Week 8: 性能和体验优化

- [ ] **Day 1-2**: 性能优化
  - [ ] 缓存实现
  - [ ] 索引优化
  - [ ] 查询优化

- [ ] **Day 3-4**: 体验优化
  - [ ] 批量操作增强
  - [ ] 快捷键支持
  - [ ] UI/UX改进

- [ ] **Day 5**: 测试验证
  - [ ] 性能测试
  - [ ] 用户体验测试

#### Week 9: 监控和扩大灰度

- [ ] **Day 1-2**: 监控告警
  - [ ] Grafana看板
  - [ ] 告警规则配置
  - [ ] 文档编写

- [ ] **Day 3-4**: 扩大灰度
  - [ ] 逐步扩大到10个用户
  - [ ] 观察指标
  - [ ] 收集反馈

- [ ] **Day 5**: 阶段总结
  - [ ] 编写优化报告
  - [ ] 制定Phase 3计划
  - [ ] 评审决策

### 4.4 验收标准

```yaml
质量提升:
  ✅ LLM提取准确率提升至 > 85%
  ✅ 人工批准率 > 85%
  ✅ 树形检索准确率 > 传统RAG 10%+

性能提升:
  ✅ 实体定位延迟 < 50ms（p95）
  ✅ 树形检索延迟 < 150ms（p95）
  ✅ 缓存命中率 > 80%

体验提升:
  ✅ 平均审核时间 < 20秒/实体
  ✅ 用户满意度 >= 4.5/5

规模验证:
  ✅ 10个用户稳定运行
  ✅ 无P0/P1级Bug
  ✅ 成本在预算内（<$30/月）
```

---

## 5. Phase 3: 自动化升级（Week 10+，持续）

### 5.1 目标

```
逐步引入自动化功能，减少人工成本，提高系统智能化程度
```

### 5.2 功能规划

#### 5.2.1 自动合并建议（Week 10-12）

```yaml
功能描述:
  - 系统自动识别可能重复的实体
  - 生成合并建议，人工确认
  - 学习人工合并模式

实现步骤:
  Week 10:
    - [ ] 合并候选发现算法
    - [ ] LLM相似度判断
    - [ ] 建议生成逻辑
  
  Week 11:
    - [ ] 前端建议展示
    - [ ] 接受/拒绝接口
    - [ ] 测试验证
  
  Week 12:
    - [ ] 灰度上线
    - [ ] 效果评估
    - [ ] 优化调整

成功指标:
  - 合并建议准确率 > 80%
  - 人工采纳率 > 60%
  - 减少50%重复实体
```

#### 5.2.2 智能消歧（Week 13-15）

```yaml
功能描述:
  - 识别不确定实体（如"小张"）
  - 持续观察，自动关联
  - 人工确认真实姓名

实现步骤:
  Week 13:
    - [ ] uncertain状态支持
    - [ ] 上下文持续收集
    - [ ] 消歧算法实现
  
  Week 14:
    - [ ] 前端确认界面
    - [ ] 关联建议展示
    - [ ] 测试验证
  
  Week 15:
    - [ ] 灰度上线
    - [ ] 效果评估
    - [ ] 文档完善

成功指标:
  - 消歧准确率 > 75%
  - 未确定实体比例 < 15%
```

#### 5.2.3 部分自动批准（Week 16+）

```yaml
功能描述:
  - 学习人工审核模式
  - 高置信度实体自动批准
  - 定期Review自动批准效果

实现步骤:
  Week 16:
    - [ ] 审核模式分析
    - [ ] 自动批准规则设计
    - [ ] 置信度阈值调优
  
  Week 17:
    - [ ] 自动批准逻辑实现
    - [ ] 人工Review机制
    - [ ] 测试验证
  
  Week 18+:
    - [ ] 小范围试点（单个用户）
    - [ ] 逐步提升自动化比例
    - [ ] 持续监控和调整

成功指标:
  - 自动批准准确率 > 95%
  - 自动化比例达到50%
  - 人工Review工作量减少50%
```

### 5.3 长期演进

```yaml
Month 6-9:
  - 关系提取自动化
  - 实体属性自动补全
  - 跨实体查询优化

Month 9-12:
  - 实体图谱可视化
  - 时间线视图
  - 智能推荐

持续优化:
  - 模型微调
  - Prompt工程
  - 性能优化
  - 成本优化
```

---

## 6. 关键指标体系

### 6.1 北极星指标

```yaml
核心指标: 树形检索准确率

定义: 
  - 用户查询意图匹配度
  - 相比传统RAG的提升幅度

目标值:
  - Phase 1: 与传统RAG持平
  - Phase 2: 提升10%+
  - Phase 3: 提升20%+

测量方法:
  - 人工评估抽样
  - 用户满意度调查
  - A/B测试对比
```

### 6.2 过程指标

#### 6.2.1 质量指标

| 指标 | Phase 1 | Phase 2 | Phase 3 | 测量方法 |
|------|---------|---------|---------|----------|
| LLM提取准确率 | >70% | >85% | >90% | 人工抽样100条 |
| 人工批准率 | >80% | >85% | >90% | 系统统计 |
| 树形检索成功率 | >90% | >95% | >98% | 系统日志 |
| 实体去重率 | - | >80% | >90% | 合并统计 |

#### 6.2.2 效率指标

| 指标 | Phase 1 | Phase 2 | Phase 3 | 测量方法 |
|------|---------|---------|---------|----------|
| 平均审核时间 | <30秒 | <20秒 | <15秒 | 用户行为日志 |
| 实体定位延迟 | <100ms | <50ms | <30ms | APM监控 |
| 树形检索延迟 | <200ms | <150ms | <100ms | APM监控 |
| 自动化比例 | 0% | 10% | 50% | 系统统计 |

#### 6.2.3 成本指标

| 指标 | Phase 1 | Phase 2 | Phase 3 | 测量方法 |
|------|---------|---------|---------|----------|
| 月LLM成本 | <$20 | <$30 | <$50 | 费用统计 |
| 人工成本 | 30分钟/天 | 20分钟/天 | 10分钟/天 | 用户反馈 |
| 存储成本 | <10GB | <50GB | <100GB | 数据库监控 |

### 6.3 监控看板

```yaml
实时看板（Grafana）:
  业务指标面板:
    - pending实体数量（实时）
    - 今日新增实体
    - 今日批准/拒绝数
    - 树形检索成功率（24小时）
  
  性能指标面板:
    - LLM调用延迟分布
    - 实体定位延迟（p50/p95/p99）
    - 树形检索延迟分布
    - 数据库慢查询
  
  成本指标面板:
    - 今日LLM调用次数
    - 今日token消耗
    - 累计LLM成本

每日报告（自动生成）:
  - 关键指标趋势
  - 异常事件汇总
  - 用户反馈摘要

每周Review（团队会议）:
  - 指标达成情况
  - 问题和改进点
  - 下周计划
```

---

## 7. 风险管理

### 7.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 | 应急预案 |
|------|------|------|----------|----------|
| LLM服务不稳定 | 中 | 高 | 重试机制、降级方案 | 暂停提取，用现有实体 |
| 数据库性能瓶颈 | 中 | 中 | 索引优化、连接池 | 扩容、读写分离 |
| ES同步延迟 | 低 | 中 | 异步批量、监控告警 | 手动触发同步 |
| 内存溢出 | 低 | 高 | 批量大小限制、监控 | 重启服务、代码优化 |

### 7.2 业务风险

| 风险 | 概率 | 影响 | 缓解措施 | 应急预案 |
|------|------|------|----------|----------|
| 用户不接受新功能 | 中 | 高 | 充分沟通、培训 | 保留传统模式 |
| 审核负担过重 | 中 | 中 | 批量操作、自动化 | 降低提取频率 |
| 实体重复严重 | 中 | 中 | 合并工具、规则优化 | 定期清理活动 |
| 成本超预算 | 低 | 中 | 实时监控、限流 | 降低提取频率、换模型 |

### 7.3 项目风险

| 风险 | 概率 | 影响 | 缓解措施 | 应急预案 |
|------|------|------|----------|----------|
| 开发进度延期 | 中 | 中 | 每日站会、及时调整 | 减少非核心功能 |
| 人员离职 | 低 | 高 | 知识文档化、结对编程 | 知识转移、临时支援 |
| 需求频繁变更 | 中 | 中 | 需求评审、版本冻结 | 推迟到下个Phase |

---

## 8. 团队和资源

### 8.1 团队组成

```yaml
核心团队（3-4人）:
  后端开发（1-2人）:
    - LLM提取服务
    - 树形检索服务
    - API开发
  
  前端开发（1人）:
    - 审核界面
    - 管理界面
  
  全栈/测试（1人）:
    - 集成测试
    - 性能测试
    - 文档编写

支持团队:
  - 产品经理: 需求澄清、用户反馈
  - UI/UX设计师: 界面设计（Phase 1前）
  - 运维工程师: 部署、监控（按需支持）
```

### 8.2 时间投入

```yaml
Phase 0（2周）:
  - 后端: 80小时
  - 测试: 20小时
  - 总计: 100小时

Phase 1（3周）:
  - 后端: 80小时
  - 前端: 80小时
  - 测试: 40小时
  - 总计: 200小时

Phase 2（4周）:
  - 后端: 60小时
  - 前端: 40小时
  - 测试: 40小时
  - 总计: 140小时

Phase 3（持续）:
  - 每月投入: 40-80小时
```

### 8.3 外部资源

```yaml
LLM服务:
  - 提供商: OpenAI / Azure OpenAI / 其他
  - 模型: GPT-4o-mini
  - 预算: $50/月（预留buffer）

云服务:
  - PostgreSQL: 现有资源
  - Elasticsearch: 现有资源
  - Redis: 现有资源，可能需要扩容

监控工具:
  - Grafana: 现有
  - Sentry: 现有
  - 日志: 现有ELK
```

---

## 9. 沟通和协作

### 9.1 会议节奏

```yaml
每日站会（15分钟）:
  - 昨天完成
  - 今天计划
  - 遇到阻碍

每周Review（1小时）:
  - 指标回顾
  - 问题讨论
  - 下周计划

Phase结束Review（2小时）:
  - 阶段总结
  - 经验教训
  - 下阶段规划

每月全员分享（1小时）:
  - 项目进展
  - 技术分享
  - 团队建设
```

### 9.2 文档规范

```yaml
必需文档:
  - [ ] 架构设计文档（已有）
  - [ ] API文档（OpenAPI）
  - [ ] 数据库Schema文档
  - [ ] 部署文档
  - [ ] 监控告警文档
  - [ ] 故障处理手册

可选文档:
  - [ ] Prompt工程文档
  - [ ] 性能优化记录
  - [ ] 用户使用手册
  - [ ] FAQ

文档位置:
  - 设计文档: services/core/docs/
  - API文档: services/rag/docs/api/
  - 运维文档: services/rag/docs/ops/
```

### 9.3 代码规范

```yaml
Code Review:
  - 所有代码必须Review后合并
  - 至少1人Approve
  - 通过CI检查

测试要求:
  - 核心逻辑单元测试覆盖率 > 80%
  - 关键接口集成测试
  - 性能测试报告

Git分支策略:
  - main: 生产环境
  - develop: 开发环境
  - feature/*: 功能分支
  - hotfix/*: 紧急修复

命名规范:
  - 遵循项目现有规范
  - 实体相关文件前缀: entity_*
  - 树形RAG相关: tree_*
```

---

## 10. 决策点和检查点

### 10.1 关键决策点

#### 决策点1: Phase 0 → Phase 1

```yaml
时间: Week 2结束
决策标准:
  ✅ LLM提取准确率 > 70%
  ✅ Worker稳定运行
  ✅ 无阻塞性技术问题

决策者: 技术负责人 + 产品经理
如果不通过: 延期1周，解决问题
```

#### 决策点2: Phase 1 → Phase 2

```yaml
时间: Week 5结束
决策标准:
  ✅ 灰度用户完整流程跑通
  ✅ 用户满意度 >= 4/5
  ✅ 无P0/P1 Bug

决策者: 产品经理 + 技术负责人 + 运营
如果不通过: 继续优化Phase 1，推迟2周
```

#### 决策点3: Phase 2 → Phase 3

```yaml
时间: Week 9结束
决策标准:
  ✅ 10个用户稳定运行1周+
  ✅ 指标达到Phase 2目标
  ✅ 成本在预算内

决策者: 产品经理 + 技术负责人 + CEO
如果不通过: 扩大灰度但暂不全量，继续优化
```

### 10.2 检查点

#### 每周检查点

```yaml
检查内容:
  - 进度是否按计划
  - 指标是否达标
  - 是否有阻塞问题
  - 是否需要资源支持

输出:
  - 周报（发送给stakeholders）
  - 风险清单更新
  - 下周计划调整
```

#### Phase结束检查点

```yaml
检查内容:
  - 所有任务是否完成
  - 验收标准是否达到
  - 文档是否齐全
  - 是否可以进入下一Phase

输出:
  - Phase总结报告
  - 经验教训文档
  - 下Phase计划
```

---

## 11. 附录

### 11.1 术语表

| 术语 | 定义 |
|------|------|
| **实体(Entity)** | 从对话中提取的结构化对象（公司、人、项目等） |
| **树形RAG** | 基于实体层级结构的检索增强生成系统 |
| **窗口(Window)** | 一组连续的消息，用于实体提取的上下文 |
| **挂载(Mount)** | Chunk与实体的关联关系 |
| **pending状态** | 待人工审核的实体状态 |
| **active状态** | 已激活可用于检索的实体状态 |
| **tree_mode** | 树形检索模式：off/shadow/boost |

### 11.2 参考文档

```yaml
设计文档:
  - 树形RAG设计方案.md
  - 实体节点生成方案-MVP.md
  - 技术栈选择.md（文档内第14章）

现有系统文档:
  - services/rag/README.md
  - services/agent/docs/Agent记忆机制与压缩方案.md

外部参考:
  - RAG最佳实践（内部知识库）
  - LLM Prompt工程指南
```

### 11.3 联系人

```yaml
项目负责人:
  - 姓名: [待定]
  - 角色: 项目PM
  - 联系方式: [待定]

技术负责人:
  - 姓名: [待定]
  - 角色: 后端Tech Lead
  - 联系方式: [待定]

产品负责人:
  - 姓名: [待定]
  - 角色: Product Manager
  - 联系方式: [待定]
```

---

## 12. 变更记录

| 版本 | 日期 | 修改人 | 修改内容 |
|------|------|--------|----------|
| v1.0 | 2024-01-XX | [姓名] | 初始版本，完整实施计划 |

---

**下一步行动**:
1. [ ] 评审本计划，确认时间线和资源
2. [ ] 分配团队角色和责任
3. [ ] 启动Phase 0开发
4. [ ] 建立项目跟踪看板（Jira/Trello）
5. [ ] 设置首次周会时间
