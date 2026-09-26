# 树形 RAG 结构 MVP 方案

> 状态：MVP 设计基线
>
> 范围：`services/rag`
>
> 目标：先完成传统 RAG 主链路，再以稳定、低耦合的方式叠加树形导航。

## 1. 核心结论

树形 RAG 不是传统 RAG 的替代品，而是在传统 RAG 之上增加的导航索引：

```text
传统 RAG
  Chunk + 全文索引 + 向量索引
  BM25 + kNN + RRF + Rerank

树形导航
  固定 Domain + 受控 Entity + Time 分桶
  输出 branch_key，用于缩小 Chunk 候选范围

Fact / Entity / KV
  后续结构化增强，不属于 MVP 的强制前置条件
```

树只回答一个问题：

> 当前查询应该优先去哪些分支查找 Chunk？

树不保存 Chunk 正文，不保存 Embedding，不作为最终证据来源。最终证据始终是经过权限校验的 Chunk。

## 2. MVP 目标

### 2.1 必须完成

1. 传统 RAG 可以独立工作。
2. Chunk 在写入索引时携带确定性的 `branch_keys`。
3. 建立 Scope、Domain、Entity、Time 四层树结构。
4. 正式 Entity 只来自受控注册表。
5. 无法确认的 Entity 进入候选表，不进入正式树。
6. 查询可以同时执行分支检索和全库检索。
7. 分支结果和全库结果通过 RRF 融合，树选错时仍可兜底。
8. 支持树关闭、影子计算和分支加权三种运行模式。
9. 跨组织和跨用户数据严格隔离。
10. 树形导航不能降低传统 RAG 的最终召回质量。

### 2.2 MVP 明确不做

1. 不依赖 LLM Fact 抽取建树。
2. 不允许 LLM 直接创建正式 Entity 节点。
3. 不做 Entity Tree 的语义摘要导航。
4. 不做 Tree Node 向量检索。
5. 不做排他性树剪枝。
6. 不做通用 KV 查询。
7. 不做事实冲突、撤销和历史版本。
8. 不要求未知实体立即人工审核。

## 3. 存储职责

### 3.1 原始来源

原始文件和附件保存在对象存储中，关系库只保存引用和元数据：

```text
resource_id
knowledge_item_id
file_name
mime_type
object_ref
content_hash
content_version
acl_version
scope
```

不支持解析的文件只保留对象存储和元数据，不生成 Chunk，也不生成 Embedding。AI 可以通过文件目录工具知道它存在，但不能检索正文。

### 3.2 Chunk

Chunk 是统一的检索证据单元：

```text
chunk_id
resource_id
knowledge_item_id
content
content_hash
content_version
sent_at
conversation_id
document_id
scope
ACL
branch_keys
```

聊天消息可以一条消息生成一个或多个 Chunk；文件按段落、页、表格或章节生成 Chunk。

### 3.3 Elasticsearch 投影

每个 Chunk 对应一个 ES 文档，同一个文档同时支持：

```text
content       -> BM25
embedding     -> kNN
branch_keys   -> 树分支过滤
metadata      -> 权限、时间、来源过滤
```

不单独创建“全文 Chunk”和“向量 Chunk”，避免正文重复。

### 3.4 树节点

树节点只保存导航信息：

```text
tree_node_id
scope_key
tree_type
node_type
parent_id
node_key
branch_key
domain
entity_id
time_bucket
status
statistics
```

节点不保存：

```text
Chunk 正文
Chunk ID 大数组
Embedding
Fact 正文
```

## 4. 树结构

推荐结构：

```text
Scope
  └─ Domain
      └─ Entity
          └─ Time
              └─ Leaf Branch
                  -> Chunk
```

### 4.1 Scope

Scope 是虚拟隔离层，至少包含一种：

```text
scope:org:{organization_id}
scope:user:{owner_user_id}
```

所有节点、Chunk、branch_key 和查询都必须携带 Scope。个人空间和组织空间不能在同一查询范围中混合。

### 4.2 Domain

Domain 表示“主体是什么类型”，在 MVP 中使用固定枚举，不允许模型新增。

建议初始 Domain：

| Domain | 含义 | 例子 |
|---|---|---|
| `organization` | 公司、部门、学校、机构 | 青云飞鹏公司、财务部 |
| `project` | 项目、活动、课程或有周期目标的事项 | 官网改版项目、2026 校招 |
| `person` | 自然人或内部成员 | 张三、李四 |
| `policy` | 制度、规范、政策 | 差旅报销制度 |
| `contract` | 合同、协议、订单 | 采购合同 A |
| `asset` | 系统、文档、表单、软件、数据集 | ERP 系统、申请表 |
| `location` | 地理或办公位置 | 郑州研发中心 |
| `unclassified` | 已确认主体，但暂时无法分类 | 无法归入上述类别的主体 |

`unclassified` 只能用于已确认主体的兜底分类，不能作为所有未知词的垃圾桶。

以下内容不应作为固定 Domain：

```text
人事
财务
行政
技术
通知
方案
重要
普通
```

这些是主题或标签，不是稳定的主体类型。

### 4.3 Entity

Entity 是“青云飞鹏公司”“项目 A”“张三”这类具体主体。

正式 Entity 只能来自受控实体注册表：

```text
entity_registry
  id
  scope_key
  domain
  canonical_name
  normalized_key
  aliases
  status
  merge_status
  created_at
  updated_at
```

正式 Entity 的创建来源：

1. 人工维护的实体表。
2. 已确认的别名。
3. 税号、身份证号、员工号等稳定标识。
4. 聊天中的稳定用户 ID、联系人 ID 或群 ID。
5. 文档元数据中已经确认的主体。

LLM 可以提出候选，但不能直接创建正式节点。

### 4.4 Time

Time 使用确定性的年月分桶：

```text
YYYY-MM
```

Time 是可选的二级分区。对于没有明确时间或时间不重要的实体，可以只写到 Entity 分支。

### 4.5 Leaf Branch

Leaf 不是 Chunk，也不是向量节点。它是一个分支谓词：

```text
entity:{entity_id}:{YYYY-MM}
```

Chunk 可以同时属于多个 Leaf：

```json
{
  "chunk_id": "chunk-1",
  "branch_keys": [
    "entity:project-a:2026-09",
    "entity:person-zhangsan:2026-09",
    "entity:contract-001:2026-09"
  ]
}
```

Chunk 正文只保存一次。

## 5. Session、Document、Time 的定位

Session、Document、Time 不作为三棵独立树。它们优先作为 Chunk 元数据过滤器：

```text
conversation_id
knowledge_item_id
document_id
sent_at
source_kind
```

当用户问：

```text
上次群里讨论过什么
某文件里怎么写的
上个月发生了什么
```

可以直接使用元数据过滤，不需要先经过树导航。

树主要负责主体导航，元数据负责会话、文档和时间过滤。

## 6. Chunk 与树的绑定

### 6.1 branch_keys

Chunk 上保存多值分支键：

```text
branch_keys text[]
```

ES Chunk 文档中保存：

```json
{
  "branch_keys": [
    "entity:project-a:2026-09",
    "entity:person-zhangsan:2026-09"
  ]
}
```

### 6.2 多分支挂载

一条消息可以同时关联多个主体：

```text
张三负责青云飞鹏公司的官网项目
```

对应：

```text
organization:青云飞鹏公司
person:张三
project:青云飞鹏官网项目
```

这三个主体可以共享同一个 Chunk。

### 6.3 Leaf 不保存 Chunk 数组

MVP 不建议在树节点上保存：

```json
{
  "chunk_ids": ["c1", "c2", "c3"]
}
```

Chunk 保存 `branch_keys` 即可。树节点只保存分支键和导航信息。

## 7. 受控实体与候选实体

### 7.1 正式实体

正式实体进入树，可以参与分支导航：

```text
entity_registry.status = active
```

### 7.2 候选实体

无法确认的实体写入候选表，不进入正式树：

```text
entity_candidates
  id
  scope_key
  candidate_name
  normalized_key
  candidate_domain
  mention_count
  distinct_source_count
  first_seen_at
  last_seen_at
  sample_contexts
  suggested_entity_id
  status
```

候选状态：

```text
new
grouped
promoted
ignored
```

### 7.3 候选晋升规则

立即晋升：

1. 命中已有实体别名。
2. 明确的稳定业务标识。
3. 用户或管理员明确确认。

进入审核池：

1. 出现在至少 2 个不同 Chunk 或来源。
2. 出现次数达到设定阈值。
3. 与已有实体相似，但没有明确别名关系。

保持候选：

1. 只出现一次。
2. 名字过于泛化。
3. Domain 无法判断。
4. 存在多个冲突候选。

### 7.4 不建立大型 unknown 树节点

不要把所有未知实体都放到一个正式 `unknown` 节点下。

未知实体：

```text
进入 entity_candidates
不进入正式树
不参与分支过滤
不影响传统 RAG
```

传统 RAG 仍然可以通过全文和向量召回这些内容。

### 7.5 审核界面原则

MVP 可以先不做审核界面。

后续审核界面应按候选实体聚合，而不是逐条展示原文：

```text
候选名称
建议 Domain
出现次数
来源数量
首次和最近出现时间
相似已有实体
推荐动作
```

人工动作：

```text
合并到已有实体
创建新实体
忽略
暂缓
```

## 8. 写入处理流程

```text
Knowledge.ready
  -> 回源
  -> 解析消息或文件
  -> Chunk
  -> 写入 Chunk
  -> 生成确定性 metadata 和 branch_keys
  -> 写入 ES Chunk 投影
  -> 基础 RAG 可检索
  -> 进行受控实体匹配
  -> 更新正式实体分支
  -> 无法确认的实体写入候选表
```

MVP 的原则：

```text
Chunk 和传统 RAG 是主链路
树导航是可选后置步骤
实体候选失败不能阻断 Chunk 检索
```

## 9. 检索流程

```text
用户问题
  -> 确定 Scope
  -> 解析时间、会话、文档和实体条件
  -> 从实体词典进行确定性匹配
  -> 命中正式 Entity 时解析 branch_keys
  -> 分支内执行 BM25 + kNN
  -> 全库执行 BM25 + kNN
  -> RRF 融合
  -> Rerank
  -> 权限复核
  -> 返回 Chunk
```

如果没有命中正式实体：

```text
不执行树过滤
直接执行传统 RAG
```

如果实体置信度不足：

```text
记录候选
不做排他过滤
最多做低权重分支加权
```

## 10. 树运行模式

MVP 支持三种模式：

### 10.1 off

完全走传统 RAG，不计算树分支。

### 10.2 shadow

计算树分支和诊断信息，但不影响最终检索结果。用于验证实体识别和分支选择是否正确。

### 10.3 boost

分支结果和全库结果同时检索，通过加权 RRF 融合。全库结果始终保留。

排他过滤不属于 MVP：

```text
filter
```

只有在测试证明高置信分支没有漏召之后，才允许针对部分查询开启。

## 11. 权限处理

权限仍然沿用传统 RAG 的授权链路：

```text
Scope 过滤
  -> ES 候选过滤
  -> Service 1 批量 Check(view)
  -> Rerank
  -> Prompt 前最终复核
```

树不能绕过权限：

1. Tree Node 必须带 Scope。
2. ES Chunk 必须带 Scope 和 ACL。
3. 分支过滤不能替代最终授权复核。
4. 树诊断日志不得输出受保护正文。
5. 跨组织和跨用户的分支不能合并。

## 12. MVP 搜索诊断

每次树检索至少记录：

```text
tree_mode
resolved_scope
resolved_entity_ids
matched_aliases
selected_branch_keys
branch_candidate_count
branch_result_count
global_result_count
fused_result_count
fallback_reason
entity_resolution_degraded
```

这些字段用于影子评测和问题定位。

## 13. 测试方案

### 13.1 数据契约测试

1. 同一 Chunk 可以挂多个 branch_key。
2. 同一实体别名归并到一个 Entity。
3. 未确认 Entity 不进入正式树。
4. 重复处理不会重复创建节点。
5. 跨 Scope 查询不会返回其他 Scope 的数据。

### 13.2 查询回归测试

测试集至少覆盖：

```text
明确实体
实体别名
多实体问题
未知实体
无实体问题
时间问题
会话语义问题
文件名或编号问题
```

每条数据标记：

```text
期望实体
期望 branch_key
gold chunk
不得出现的 Scope
```

### 13.3 影子评测

同一批查询分别执行：

```text
traditional_rag
tree_shadow
tree_boost
```

比较指标：

```text
Recall@k
MRR
nDCG
branch_hit_rate
wrong_branch_rate
branch_reduction_ratio
p50/p95 latency
```

### 13.4 验收要求

1. `tree_boost` 的最终 Recall 不低于 `traditional_rag`。
2. 未识别实体时必须回退全库检索。
3. 跨 Scope 泄露必须为零。
4. 候选实体不能进入正式树。
5. 树选错时全库通道仍能返回正确答案。
6. 数据量较小时允许树没有性能收益，但不能引入质量回退。

## 14. 重构范围

### 14.1 新主链路

```text
Source
  -> Chunk
  -> ES 全文和向量投影
  -> 确定性 branch_keys
  -> 受控实体导航
```

### 14.2 旧链路处理

现有 `memory_*` 写路径和 Fact 建树不应继续作为主链路增量维护。

建议：

1. 冻结旧写入逻辑。
2. 新链路不写旧 Fact/Node 结构。
3. 从 Knowledge 重新回放构建 RAG 数据。
4. 需要兼容 API 时，在接口层做转换，不在存储层继续堆兼容分支。

## 15. 后续阶段

### 阶段 2：语义节点

1. Domain 和 Entity 节点摘要。
2. 节点向量辅助选择分支。
3. 仍然不能替代 Chunk 召回。

### 阶段 3：Fact

1. 从 Chunk 异步抽取 Fact。
2. Fact 关联多个 Chunk 来源。
3. Fact 支持状态、版本和冲突。
4. Fact 可以挂到 Entity 分支，但不是 Chunk 入树的前置条件。

### 阶段 4：KV

1. 建立受控 key registry。
2. 支持精确字段查询。
3. KV 指向 Fact 和 Chunk。
4. KV 必须继承来源 ACL。

## 16. 最终原则

1. 传统 RAG 必须可以独立运行。
2. 树是导航索引，不是正文或向量存储。
3. Leaf 逻辑上指向 Chunk，物理上只提供 branch_key。
4. Session、Document、Time 优先作为过滤字段，不拆成独立树。
5. Domain 固定，Entity 受控。
6. LLM 只能提出候选，不能直接创建正式节点。
7. 未知实体进入候选表，不进入大型 unknown 节点。
8. 树识别不确定时，不做排他过滤。
9. 分支检索和全库检索并行，RRF 融合。
10. Fact、Entity 和 KV 是后续增强层，不是 MVP 的必经链路。

一句话总结：

> MVP 的树是一套受控主体导航目录。它建立在传统 RAG 之上，只负责给 Chunk 检索提供确定性分支；传统 RAG 始终是主链路和兜底通道。
