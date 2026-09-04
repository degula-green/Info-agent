# 信息管理 Agent 三模块边界

本文档基于当前需求和第一期范围，定义三个逻辑模块：

```text
模块一：IAM / 组织与权限
模块二：连接器、消息采集与知识管理
模块三：信息处理、检索与问答
```

三个模块可以按需要部署在一个或多个进程、服务或代码库中；本文档只约束逻辑职责和调用边界，不预设物理部署方式。

## 一、模块一：IAM / 组织与权限

### 负责什么

- 邮箱注册、登录、JWT、会话和当前用户身份。
- 组织创建、邀请加入、成员管理、多组织切换。
- 组织角色和成员关系，例如组织创建人、信息管理员、审批员、普通成员。
- 维护统一的 OpenFGA authorization model。
- 提供统一的资源访问检查、关系写入/撤销和审计接口。

### 不负责什么

- 不调用飞书、企业微信或个人微信接口。
- 不执行消息采集、清洗、规范化或隐私识别。
- 不判断某条消息属于哪个群聊或知识库。
- 不解析附件、不建 ES 索引、不生成 RAG 回答。

### OpenFGA 边界

模块一拥有权限模型和统一鉴权入口，但不理解平台消息内容。模块二根据消息上下文计算“应该有哪些关系”，通过模块一的内部接口写入或撤销关系。OpenFGA 只保存关系，不保存正文、敏感原因或完整隐私标签。

典型接口：

```text
CheckAccess(subject, resource, action)
BatchWriteRelations(relations)
BatchDeleteRelations(relations)
RecordAudit(...)
```

资源权限动作只有 `view` 和 `download`，不使用一个笼统的 `canAccess` 代替。“共享私聊”是模块二的业务操作，不是资源权限动作。

模块一对外只接受用户主体；后台服务身份通过 `caller_service` 认证，不作为 OpenFGA 资源主体。统一 Access Check 使用 `resource_part` 区分 `display/original/metadata/content`：展示内容映射到 `knowledge_item`，敏感原文映射到 `knowledge_original`，附件元数据映射到 `attachment_meta`，附件内容查看和下载映射到 `attachment_content`。本地表格按附件处理，第一期没有独立的 `spreadsheet` 权限资源类型。

模块一必须校验关系写入白名单：同一资源的 `owner`、`organization`、`conversation_group` 基础范围互斥；群聊只接收 `participant` 和唯一 `organization`，有效成员由 OpenFGA 计算为平台参与者与当前组织成员的交集；父资源关系必须指向正确对象。普通附件写 `accessor`，受保护群聊附件不得写 `accessor`。资源删除时撤销基础关系，数据库生命周期由业务接口检查。

知识库展示与归属约定：私人知识库包含私人私聊会话和私人本地文件库；公司知识库按群聊展示消息及该群聊文件，并另设一个公司文件库，汇总所有群聊文件和公司本地上传文件。汇总展示不改变资源权限：群聊文件仍按 `conversation_members` 独立鉴权，公司本地上传文件按 `organization_members` 鉴权。

## 二、模块二：连接器、消息采集与知识管理

模块二合并原需求中的平台连接器、信息采集和知识内容/归属模块，但内部仍划分为：

```text
connector → ingestion → normalization → knowledge → security → delivery
```

### 负责什么

#### 连接器

- 外部平台账号绑定、OAuth 或平台授权。
- 外部账号与内部用户映射。
- 平台 token 的安全保存、刷新和失效处理。
- 平台会话、群聊、消息、附件的外部 ID 管理。

#### 采集

- 定时轮询平台消息，不依赖 Webhook。
- 保存每个采集源、组织和会话的检查点。
- 采集游标/时间点、断点、重试和任务状态。
- 有效性过滤和 Redis 快速判重。
- 数据库唯一约束提供最终幂等保障。

第一期只处理新增消息快照：暂不处理平台消息编辑、撤回/删除，也暂不同步协作表格的后续编辑。

#### 规范化与知识

- 把飞书、企业微信、个人微信等平台消息转换为统一格式。
- 保存消息、正文引用、附件元数据、会话、发送人、采集者和组织关系。
- 判断个人知识库/组织知识库归属和共享关系。
- 接收本地附件上传，并根据上传目标创建私人或组织知识对象。
- 管理私人私聊中被用户混合选中的文字、文件、图片等消息到当前绑定组织的一次性共享。
- 维护知识对象生命周期、内容版本和处理状态。

#### 隐私与权限编排

- 只判断组织群聊消息或附件是否需要隐私识别。
- 异步执行组织群聊敏感信息识别并保存敏感等级、识别状态和策略版本。
- 根据知识来源确定私人所有者、组织成员或群聊成员访问范围。
- 私人聊天、私人本地上传、公司本地上传和共享私聊直接标记为无需识别；公司本地上传和共享私聊不脱敏且不进入审批限制。
- 组织群聊敏感文本生成脱敏展示内容并保护原文；受保护群聊附件只开放元数据，内容查看和下载分别审批。
- 调用模块一写入或撤销 OpenFGA 关系。
- 只有权限准备完成后，才向模块三发布可处理事件。

### 不负责什么

- 不定义全系统 OpenFGA model。
- 不直接让模块三写入 ES 或向量库。
- 不负责切块、Embedding、召回、重排或 LLM 回答。
- 不把平台原始字段直接暴露给模块三作为长期契约。

### 采集检查点

检查点不是只有一个全局时间点，至少按以下维度保存：

```text
source_id
organization_id
owner_user_id（私人会话必填）
conversation_id
conversation_type
last_cursor 或 last_success_at
```

同一个平台账号加入多个群时，每个群可以有独立进度。私人会话按 `source_id + owner_user_id + conversation_id` 维护。若平台只有全局游标，仍必须保存消息所属的 `conversation_id`。

### 统一消息对象

```json
{
  "message_id": "msg_internal_001",
  "organization_id": "org_001",
  "owner_user_id": null,
  "knowledge_scope": "organization",
  "access_scope": "conversation_members",
  "source_platform": "feishu",
  "external_message_id": "om_123",
  "external_conversation_id": "oc_456",
  "sender_identity_id": "identity_100",
  "message_type": "text",
  "content_ref": "object://messages/msg_internal_001",
  "sent_at": "2026-09-03T10:00:00+08:00",
  "collected_at": "2026-09-03T10:05:00+08:00",
  "content_hash": "sha256:xxx",
  "content_version": 1,
  "lifecycle_status": "active",
  "security_status": "pending"
}
```

`content_version` 用于异步任务、索引和权限结果的一致性，并为未来编辑/撤回预留；它本身不等于编辑或撤回机制。`content_hash` 用于判断内容是否实际变化。

### 本地附件上传

本地上传不是平台轮询，不创建平台采集检查点。用户选择私人本地知识库或公司文件库，模块二使用独立上传任务记录状态。文件二进制写入对象存储，Redis 只保存任务 ID、资源 ID 和处理事件。

接口为 `POST /knowledge/attachments`，上传元数据使用 `attachment-upload.schema.json`。公司文件库目标使用当前用户绑定的组织，不允许客户端指定其他组织。

```text
用户选择上传目标并提交附件
→ 模块二创建上传任务 pending
→ 公司目标由模块一确认当前用户属于组织
→ 校验文件类型、大小并计算 content_hash
→ Redis 快速判重
→ 数据库唯一键最终幂等
→ 新文件写入对象存储
→ 保存附件元数据和 KnowledgeItem
→ 写入固定权限
→ 写入 document.processing.requested Outbox
→ 投递 Redis Streams
→ 模块三解析、切块、向量化和建立索引
```

私人上传创建 `knowledge_scope=private`、`access_scope=owner_only` 的 KnowledgeItem，保存 `owner_user_id`，读取和下载只允许所有者。公司上传前必须由模块一确认当前用户属于绑定组织，创建 `knowledge_scope=organization`、`access_scope=organization_members` 的 KnowledgeItem；公司成员默认可查看和下载，不为每个成员逐条配置文件权限。重复判断范围分别为 `owner_user_id + content_hash` 和 `organization_id + content_hash`。

### 私聊按条共享

私聊默认保存为私人知识，用户可以在同一次操作中混合选择同一私聊中的文字、文件、图片等一条或多条消息，共享到当前用户所属组织；不支持持续共享，后续新消息不会自动共享：

接口为 `POST /knowledge/private/shares`，请求和响应分别使用 `private-conversation-share-request.schema.json` 与 `private-conversation-share-response.schema.json`。组织 ID 由当前用户绑定关系确定。

共享请求按 `request_id` 幂等；同一批次内按 `share_batch_id + source_private_item_id` 防止重复创建组织引用。未出现在 `message_ids` 中的文字、文件或图片消息均不创建组织引用；撤销共享不在第一期范围内。

```text
用户开启“共享到公司”
→ 模块二确认私聊属于当前用户
→ 模块一确认用户属于绑定组织
→ 创建 share_batch_id
→ 写入 private.conversation.share.requested Outbox
→ 文字消息创建组织 KnowledgeItem 逻辑引用
→ 文件或图片消息创建组织 KnowledgeItem，并为该消息自身的文件创建组织逻辑附件引用
→ 为本次明确选中的消息资源登记 view/download 权限
→ 发布 knowledge.ready，模块三建立组织侧索引
```

系统只共享 `message_ids` 中明确选中的消息，不根据消息的时间、上下文或相邻关系自动附带其他消息。文字、文件和图片消息可以在同一批次中混合选择。私人原件继续保持 `owner_only`；组织引用只保存新的组织归属和引用元数据，复用原正文、物理文件和解析结果，不复制底层数据。文件或图片消息使用新的组织逻辑附件 ID，避免扩大私人文件本身的权限。共享是一次性业务操作，不是 `share` 权限。

## 三、模块三：信息处理、检索与问答

### 负责什么

- 消费模块二发布的 `knowledge.ready` 等事件。
- 消费 `document.processing.requested`，完成附件预处理并返回提取结果。
- 附件预处理和 MinerU 解析。
- 根据 `attachments[].content_access_required` 阻止受保护附件的原始解析文本进入普通索引。
- 文本切块、Embedding、全文/向量索引。
- Elasticsearch、多路召回、混合检索和重排。
- 搜索历史、RAG 上下文构造、LLM 回答和 QA 会话记录。
- 搜索和问答过程中调用模块一进行权限过滤。

### 不负责什么

- 不绑定外部平台账号，不轮询消息。
- 不判断消息归属，不决定敏感等级。
- 不自行定义另一套权限模型或直接修改组织成员关系。
- 不绕过模块一返回没有权限的正文、附件或问答上下文。

模块三保存索引时必须带上：

```text
organization_id
knowledge_item_id
content_version
acl_version
```

## 四、模块之间的调用关系

```text
前端 → 模块一：登录、组织、角色、权限申请
前端 → 模块二：连接器、采集任务、知识库和消息详情
前端 → 模块三：搜索、问答、搜索历史

模块二 → 外部平台：授权、定时轮询、消息/附件获取
模块二 → 模块一：OpenFGA 关系写入/撤销、权限检查
模块二 → 模块三：发布 document.processing.requested、knowledge.ready 等事件
模块三 → 模块一：搜索/问答候选内容的访问检查
模块三 → 模块二：按引用读取已授权的知识内容或附件
```

模块三不直接读取模块二的内部表。跨模块优先使用事件和稳定查询接口，不共享内部表结构。

## 五、消息采集流程

当前第一期的新增消息流程：

```text
1. 定时任务触发
2. 模块二读取 source、organization、conversation 的采集检查点；私人会话额外读取 owner_user_id 维度
3. 连接器调用外部平台消息列表接口
4. 有效性过滤：空消息、无业务价值的通话通知等丢弃
5. Redis 快速判重
6. 数据库唯一键最终幂等
7. 平台消息规范化为 UnifiedMessage
8. 保存私人或组织消息、附件元数据、会话、发送人和知识归属
9. 私人会话只保存私人消息；按条共享由独立的用户请求触发，采集时不自动创建组织引用
10. 仅组织群聊文本写入 privacy.scan.requested Outbox；附件写入 document.processing.requested
11. 组织群聊附件由模块三预解析后执行隐私识别；其他附件只解析并直接设置 security_status=not_required
12. 普通群聊附件写入群成员 accessor；受保护群聊附件不写 accessor，等待 view/download 审批
13. 模块二调用模块一登记消息、KnowledgeItem、附件及父资源关系
14. content、ownership、security、permission 均 ready
15. 模块二发布 knowledge.ready
16. 模块三读取指定版本，解析、切块、向量化并建立索引
```

Redis 只是性能优化；数据库唯一键才是最终幂等保障。采集检查点必须在消息和 Outbox 成功持久化后再推进，避免先推进检查点造成漏采。

## 六、隐私识别与 Outbox

组织群聊的“写入隐私识别任务”和“异步执行隐私识别”是两个阶段：

```text
保存消息时：创建 privacy_scan_task，状态为 pending
worker 取任务：pending → processing
worker 分析正文/OCR/附件文本
保存 sensitivity、classification_status、policy_version
processing → succeeded 或 failed
```

推荐在一个数据库事务中完成：

```text
保存统一消息
保存消息归属
写入 privacy.scan.requested Outbox 记录
```

`privacy.scan.requested` 只是“组织群聊任务已登记并等待执行”，不是识别完成。Outbox 保证消息保存成功后，隐私任务不会因为进程崩溃而丢失，并支持重试、扩展多个 worker 和审计追踪。私人聊天、本地上传和共享私聊不写入该事件，直接设置 `security_status=not_required`。

访问范围首先由资源来源确定：

```text
私人知识和私人本地上传       owner_only
公司本地上传和共享私聊       organization_members
公司群聊消息及群聊文件       conversation_members
```

公司本地上传和共享私聊不执行隐私识别或脱敏，始终按 `organization_members` 开放查看和下载。组织群聊文本可生成敏感标签、审计记录和脱敏内容；普通群聊附件按群成员开放，受保护群聊附件只展示元数据并对内容查看、下载分别审批。

## 七、就绪状态与交接闸门

模块二可以使用以下状态表示是否允许交给模块三：

```text
content_saved    内容正文或附件元数据已稳定保存
ownership_ready  组织、会话、发送人、知识库等归属已确定
security_ready   隐私识别已完成，或明确无需识别
permission_ready KnowledgeItem 和附件的 view/download 关系已同步
```

只有：

```text
content_saved
  && ownership_ready
  && security_ready
  && permission_ready
```

才发布 `knowledge.ready`。包含多个附件时，正文和全部附件都必须完成保存、所需预处理、隐私处理和权限登记；任一附件失败或未完成，都不发布 `knowledge.ready`。隐私识别或权限同步失败时，默认禁止内容进入检索和问答。

## 八、第一期明确不做

- 不使用 Webhook。
- 不处理已采集消息的编辑。
- 不处理已采集消息的撤回/删除。
- 不同步协作表格停止编辑后的后续版本；只保存轮询时获得的快照。
- 本地上传第一期只支持附件，可选择私人本地知识库或公司文件库。
- 私聊共享第一期允许一次混合选择文字、文件和图片消息，只共享 `message_ids` 中明确选中的消息；后续新消息不自动共享。
- 不要求每条消息进入后立即同步完成索引；隐私识别、权限同步和 RAG 处理均可异步。

仍建议保留以下字段，为后续能力预留接口：

```text
external_message_id
content_hash
content_version = 1
lifecycle_status = active
platform_updated_at（可为空）
```

## 九、边界判断原则

```text
模块一回答：谁是谁、属于哪个组织、能否访问资源？
模块二回答：平台有什么有效消息、消息归属什么知识、应登记哪些访问关系？
模块三回答：如何处理知识、如何检索，以及如何生成问答？
```

核心原则：**模块二产生资源权限事实，模块一统一管理和执行权限，模块三只消费已经完成归属和权限准备的知识对象。**
