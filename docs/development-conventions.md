# 三模块开发约定

本文档约束三个逻辑模块之间的联调协议，不规定未来必须拆成几个服务、进程或代码库。

```text
模块一：IAM / 组织与权限
模块二：连接器、消息采集与知识管理
模块三：信息处理、检索与问答
```

## 1. 协议总则

- 同步查询、权限检查和资源读取使用 HTTP。
- 异步任务和跨模块通知使用 Redis Streams；使用消费组、至少一次投递和消费者幂等。
- HTTP 与事件的数据结构以 `docs/contracts/` 下的 JSON Schema 为准。
- 外部请求携带用户 JWT；后台任务携带内部调用身份 `caller_service`，并显式携带组织和资源上下文。
- 时间统一使用 ISO 8601 UTC；公共 ID 使用字符串，不传递数据库自增 ID 作为跨模块标识。
- 正文和附件使用 `content_ref`、`object_ref` 或资源 ID，不在事件中传输大文件二进制内容。

## 2. 通用请求上下文

跨模块 HTTP 请求应包含：

```json
{
  "request_id": "req_001",
  "trace_id": "trace_001",
  "organization_id": null,
  "actor_id": "user_001",
  "caller_service": "module-2"
}
```

`organization_id` 不得由接收方从资源或当前会话猜测。私人知识请求使用 `null`，组织知识请求必须填写组织 ID。`actor_id` 是实际用户；纯后台任务可为空，但必须有 `caller_service`。

## 3. 模块一接口约定

模块一拥有统一权限模型和鉴权入口，提供：

```text
POST /internal/access/check
POST /internal/access/relations:batch-write
POST /internal/access/relations:batch-delete
POST /internal/access/audit
```

权限动作只建模两种：`view` 和 `download`。附件是独立资源，不能默认继承消息权限；“共享私聊”是业务操作，不是权限动作。

模块二根据资源来源确定访问范围，并调用模块一写入或撤销 OpenFGA 关系；模块一负责校验调用方、执行 OpenFGA、返回 `acl_version` 并记录审计。模块三只能调用 Check，不得自行定义或修改权限模型。

访问范围固定为：私人知识 `owner_only`；公司本地上传和共享私聊为 `organization_members`；公司群聊消息及文件为 `conversation_members`。公司本地上传不需要逐用户配置文件 ACL，但访问时仍须校验组织成员身份。

知识库展示与归属约定：私人知识库包含私人私聊会话和私人本地文件库；公司知识库按群聊展示消息及该群聊文件，并另设一个公司文件库，汇总所有群聊文件和公司本地上传文件。汇总展示不改变资源权限：群聊文件仍按 `conversation_members` 独立鉴权，公司本地上传文件按 `organization_members` 鉴权。

## 4. 模块二的统一知识对象

模块二输出给模块三的对象为 `KnowledgeItem`：

```json
{
  "knowledge_item_id": "ki_001",
  "knowledge_scope": "organization",
  "access_scope": "conversation_members",
  "owner_user_id": null,
  "organization_id": "org_001",
  "source": {
    "source_type": "platform_conversation",
    "platform": "feishu",
    "conversation_id": "conv_001",
    "external_message_id": "msg_ext_001",
    "sender_identity_id": "identity_001",
    "upload_destination": null
  },
  "content_ref": "knowledge://ki_001",
  "content_type": "text",
  "content_hash": "sha256:xxx",
  "content_version": 1,
  "lifecycle_status": "active",
  "security_status": "classified",
  "sensitivity": "confidential",
  "acl_version": 3,
  "attachments": []
}
```

- `content_version` 用于异步任务、权限结果和索引一致性；当前第一期固定为 `1`。
- `content_hash` 用于判断内容是否实际变化。
- `lifecycle_status` 当前只使用 `active`，为未来编辑/撤回预留。
- `security_status` 表示隐私识别状态；`sensitivity` 表示识别结果。
- `acl_version` 对应模块一当前权限关系版本。
- `knowledge_scope` 表示知识属于私人知识库还是组织知识库；`access_scope` 表示实际访问范围。
- `upload_destination` 只用于本地上传，可为 `private_local_library` 或 `organization_file_library`。
- 共享私聊的组织引用通过 `sharing` 记录来源，不代表新增的权限动作。

模块三通过稳定接口读取对象和内容：

```text
GET /internal/knowledge/{knowledge_item_id}
GET /internal/knowledge/{knowledge_item_id}/content
GET /internal/attachments/{attachment_id}
```

模块三不得直接读取模块二数据库、调用外部平台或修改知识归属/隐私等级。

## 5. Redis Streams 事件约定

所有事件使用统一信封：

```json
{
  "event_id": "evt_001",
  "event_type": "knowledge.ready",
  "schema_version": 1,
  "occurred_at": "2026-09-03T10:00:00Z",
  "trace_id": "trace_001",
  "organization_id": "org_001",
  "producer": "module-2",
  "payload": {
    "knowledge_item_id": "ki_001",
    "content_version": 1,
    "access_scope": "conversation_members"
  }
}
```

第一期事件：

```text
privacy.scan.requested
document.processing.requested
document.extracted
private.conversation.share.requested
knowledge.ready
processing.completed
processing.failed
```

约定：`event_id` 是幂等键；消费成功后再 ACK；失败先重试，超过次数进入死信队列；事件处理必须可重复执行。`knowledge.ready` 只有在内容、归属、隐私和权限均 ready 后才能发布。事件 payload 只传任务 ID、资源 ID、版本和引用地址，不传大正文或文件二进制。

## 6. 消息采集与交接流程

当前使用定时轮询，不使用 Webhook：

```text
1. 模块二读取 source、organization、conversation 的采集检查点；私人会话额外按 owner_user_id 维度维护。
2. 连接器调用外部平台消息列表接口。
3. 过滤空消息、无业务价值的通话通知等无效内容。
4. Redis 快速判重，数据库唯一键做最终幂等。
5. 将平台消息规范化为统一对象。
6. 保存消息、附件元数据、会话、发送人和知识归属。
7. 文本消息在同一事务写入 privacy.scan.requested Outbox；带附件的消息写入 document.processing.requested。
8. 附件由模块三预解析并返回 document.extracted，模块二再对提取文本执行隐私识别。
9. 模块二根据消息来源确定固定访问范围；隐私结果只用于标签、审计、脱敏或阻断处理。
10. 模块二调用模块一登记消息、KnowledgeItem 和附件的访问关系。
11. 所有就绪条件满足后发布 knowledge.ready。
12. 模块三读取指定版本，解析、切块、向量化并建立索引。
13. 模块三发布 processing.completed 或 processing.failed。
```

组织会话检查点至少按 `source_id + organization_id + conversation_id` 维护；私人会话至少按 `source_id + owner_user_id + conversation_id` 维护，并保存 `last_cursor` 或 `last_success_at`。本地上传没有平台检查点，使用独立上传任务状态。检查点只能在消息和 Outbox 成功持久化后推进。

### 本地上传

本地上传使用模块二的 HTTP 入口，用户选择 `private_local_library` 或 `organization_file_library`。文件二进制写入对象存储，Redis 只传任务事件和资源引用。

```text
POST /knowledge/attachments
```

上传元数据使用 `attachment-upload.schema.json`；公司目标的组织 ID 由当前用户绑定的组织确定，私人目标的组织 ID 为 `null`。

```text
用户选择目标并上传文件
→ 模块二创建上传任务 pending
→ 公司目标由模块一确认当前用户属于组织
→ 校验文件类型、大小并计算 content_hash
→ Redis 快速判重
→ 数据库唯一键做最终幂等
→ 文件写入对象存储
→ 保存附件元数据和 KnowledgeItem
→ 写入固定权限
→ 写入 document.processing.requested Outbox
→ Redis Streams 投递处理任务
→ 模块三解析、切块、向量化并建立索引
```

私人目标创建 `knowledge_scope=private`、`access_scope=owner_only` 的 KnowledgeItem，只记录 `owner_user_id`，不执行组织权限同步。读取和下载仍须校验当前用户是所有者。

公司目标先由模块一确认当前用户属于绑定组织，再创建 `knowledge_scope=organization`、`access_scope=organization_members` 的 KnowledgeItem。公司成员默认可以查看和下载，不为每个成员逐条写文件权限。重复判断分别使用 `owner_user_id + content_hash` 或 `organization_id + content_hash`。

### 私聊按条共享

私聊默认创建私人 KnowledgeItem，仅所有者可查看和下载。用户可以在同一次操作中混合选择同一私聊中的文字、文件、图片等一条或多条消息，共享到当前用户绑定的组织；不支持持续共享，后续新消息不会自动共享：

```text
用户开启“共享到公司”
→ 模块二校验私聊属于当前用户
→ 模块一确认用户属于绑定组织
→ 创建 share_batch_id
→ 写入 private.conversation.share.requested Outbox
→ 文字消息创建组织 KnowledgeItem 逻辑引用
→ 文件或图片消息创建组织 KnowledgeItem，并为该消息自身的文件创建组织逻辑附件引用
→ 为本次明确选中的消息资源登记 view/download 权限
→ 发布 knowledge.ready，模块三建立组织侧索引
```

系统只共享 `message_ids` 中明确选中的消息，不根据消息的时间、上下文或相邻关系自动附带其他消息。文字、文件和图片消息可以在同一批次中混合选择；文件或图片消息使用新的组织逻辑 `attachment_id`，通过来源字段指向私人文件并复用同一 `object_ref`。组织引用只复制逻辑元数据和归属，不复制正文、物理文件或解析结果；私人原件仍保持 `owner_only`。共享关系是一次性业务操作，不是 `share` 权限。

共享接口为 `POST /knowledge/private/shares`，请求和响应分别使用 `private-conversation-share-request.schema.json` 与 `private-conversation-share-response.schema.json`。请求提交私聊会话 ID 和选中的消息 ID，组织 ID 由服务端从当前用户绑定关系确定。

共享请求按 `request_id` 幂等；同一批次内按 `share_batch_id + source_private_item_id` 防止重复创建组织引用。未出现在 `message_ids` 中的文字、文件或图片消息均不创建组织引用；撤销共享不在第一期范围内。

## 7. 隐私识别与权限状态

保存消息时只登记任务，不同步等待识别：

```text
privacy_scan_task: pending → processing → succeeded / failed
```

`privacy.scan.requested` 表示任务已可靠登记，不表示识别完成。访问范围先由资源来源确定：私人内容为 `owner_only`，公司本地上传和共享私聊为 `organization_members`，群聊消息及文件为 `conversation_members`。隐私结果只用于敏感标签、审计、脱敏或阻断处理，不改变上述成员范围。OpenFGA 保存关系，不保存正文或隐私原因。

模块二在交给模块三前维护以下闸门：

```text
content_saved    pending | ready | failed
ownership        pending | ready | failed
security         not_required | pending | processing | classified | failed
permission       pending | syncing | ready | failed
processing       blocked | pending | processing | completed | failed
```

只有 `content_saved=ready`、`ownership=ready`、`security` 为 `not_required/classified` 且 `permission=ready` 时，才允许发布 `knowledge.ready`。失败或未完成时默认禁止敏感内容进入检索和问答。

## 8. 错误、幂等和版本

统一错误结构：

```json
{
  "code": "ACCESS_DENIED",
  "message": "resource access denied",
  "request_id": "req_001",
  "retryable": false,
  "details": {}
}
```

可重试：超时、连接失败、Redis 暂时不可用、模型服务暂时不可用。不可重试：资源不存在、字段校验失败、无权限、组织不匹配。

消费者必须按 `event_id` 幂等，并按 `knowledge_item_id + content_version` 防止旧任务覆盖新索引。模块三索引至少保存 `organization_id`、`knowledge_item_id`、`content_version`、`acl_version`。

## 9. 用户访问流程

```text
查看消息：前端 → 模块二 → 模块一 Check(view) → 返回原文/脱敏内容/拒绝
下载附件：前端 → 模块二 → 模块一 Check(download) → 生成临时地址并记录审计
搜索：前端 → 模块三 → 模块一批量 Check(view) → 权限过滤后返回
问答：前端 → 模块三 → 检索候选 → 模块一 Check(view) → 过滤上下文 → LLM 回答
```

模块三不能只在前端隐藏无权结果；搜索和问答的权限过滤必须发生在服务端。

## 10. 第一版不做

- 不使用 Webhook。
- 不处理已采集消息的编辑、撤回和删除。
- 不同步协作表格的后续版本，只保存轮询时获得的快照。
- 不要求同步完成索引，隐私识别、权限同步和 RAG 处理均可异步。
- 不在模块三实现新的权限模型。
- 不在事件中传输大正文或大附件二进制内容。

仍保留 `external_message_id`、`content_hash`、`content_version=1`、`lifecycle_status=active` 和可为空的 `platform_updated_at`，为后续版本能力预留。

## 11. 联调验收

1. 合法 `knowledge.ready` 可被模块三消费并建立索引。
2. 重复 `event_id` 不产生重复索引。
3. 权限未 ready 时模块二不发布 `knowledge.ready`。
4. 模块一拒绝访问时模块三不返回正文或问答上下文。
5. 处理失败产生 `processing.failed` 并按规则重试。
6. 组织不匹配请求被拒绝。
7. 附件下载与消息查看分别鉴权。
8. 旧 `content_version` 任务不能覆盖新版本索引。
9. 私人本地上传只能由所有者查看和下载，公司本地上传只能由绑定组织成员查看和下载。
10. 私聊按条共享允许混合选择文字、文件和图片消息，只为 `message_ids` 中的消息产生组织逻辑引用，且不复制底层正文和文件。
