# 信息管理 Agent 数据库设计第二版

> 版本：v0.2（修订版）  
> 数据库：PostgreSQL  
> 设计范围：IAM / 组织与权限、连接器 / 采集 / 知识管理、信息处理 / 检索 / 问答三个独立微服务  
> 本文是字段级评审稿，不是最终建表 SQL。相较初版由 42 张表压缩为 24 张表。

## 1. 总体设计

### 1.1 Schema 与服务归属

| Schema | 所属微服务 | 数据所有权 |
|---|---|---|
| `iam` | IAM / 组织与权限服务 | 用户、认证、组织、成员角色、邀请、访问申请、OpenFGA 编排与权限审计 |
| `knowledge` | 连接器 / 采集 / 知识管理服务 | 个人连接器、已接入会话的外部身份、采集、消息、附件、知识条目、隐私状态和可靠事件 |
| `rag` | 信息处理 / 检索 / 问答服务 | 解析与索引任务、ES 索引映射、搜索历史、问答会话和消息 |

三个服务共用一个 PostgreSQL 实例，但每个服务只拥有自己 Schema 的 DDL 和写权限。跨服务优先使用 HTTP 与 Redis Streams；确需跨 Schema 查询时，只开放经过评审的只读视图，不允许跨 Schema 写表或建立业务外键。

### 1.2 通用约定

1. 主键统一使用 `UUID`，默认 `gen_random_uuid()`；跨服务传递字符串形式的 UUID。
2. 时间统一使用 `TIMESTAMPTZ`，跨服务协议使用 ISO 8601 UTC。
3. 状态枚举使用 `VARCHAR + CHECK`，避免 PostgreSQL ENUM 增加迁移耦合。
4. 同一 Schema 内建立外键；跨 Schema 字段仅保存逻辑 ID。
5. MinIO 保存正文、脱敏正文、附件和解析产物；PostgreSQL 保存引用、元数据、状态与审计；Elasticsearch 保存可检索文本、向量和过滤字段。
6. JWT 采用无状态访问令牌；短期登录态、Refresh Token 黑名单和设备撤销信息由 Redis 管理，一期不在 PostgreSQL 建立登录会话表。需要长期设备审计时再增加独立会话存储。
7. Redis 只用于登录态缓存、快速判重和事件传输，PostgreSQL 唯一约束和 Outbox 才是知识数据最终幂等与可靠性依据。
8. 所有业务表均应包含必要的 `created_at`；可变聚合增加 `updated_at`。软删除仅用于确实需要保留历史的主体。

### 1.3 一期核心约束

- 一期一个用户最多存在一个有效组织成员关系；结构保留未来多组织能力。
- 一个组织可以有多个 Owner；同一成员可以同时拥有多个固定角色；Owner 可以转让。
- 一期角色代码固定为 `owner`、`information_admin`、`membership_approver`、`member`，不单独建立角色定义表。
- 连接器归个人，同一平台租户下的同一外部账号不能被不同内部用户重复绑定。
- 外部平台的会话发现列表不写入 PostgreSQL；只有用户确认接入后才创建 `conversation_ingestions`，并以它为所有会话数据的根记录。私聊只能接入个人知识库，群聊才可以接入组织知识库。
- 同一外部群聊同一时刻只能被一个组织接入；解除后可由其他组织重新接入。
- 一个接入有一个主采集者和多个补充采集者；没有有效采集者时暂停采集。
- 群聊成员使用 OpenFGA 动态用户组；加入后可访问历史，退出群聊或组织后立即失去通过该组获得的权限。
- 本地上传一期仅支持附件，目标可选择私人本地知识库或公司文件库。
- 资源动作只有 `view` 和 `download`；共享和审批属于业务流程，不是 OpenFGA 动作。
- 普通或脱敏文本可按基础访问范围查看；敏感原文必须申请 `view`。
- 受保护附件只展示文件存在及元数据；查看内容或下载文件必须分别取得 `view` / `download` 授权。
- 私人知识一期不创建访问申请；所有者直接拥有私人资源的 `view/download`。数据库保留私人申请扩展字段，但当前申请接口只处理组织资源。
- 个人可以在同一次操作中混合选择一条或多条文字、文件、图片等私聊消息共享到组织；组织逻辑副本默认对该组织成员可 `view/download`，未选择的消息不进入组织知识库。
- 一期不处理已采集消息的编辑、撤回和删除，`content_version` 初始固定为 `1`。

### 1.4 敏感内容资源分层

OpenFGA 不保存正文、脱敏结果或敏感原因，只保存主体与资源的关系。逻辑资源分为：

| OpenFGA 对象类型 | 数据来源 | 默认展示 | 需要申请的动作 |
|---|---|---|---|
| `knowledge_item:{id}` | `knowledge.knowledge_items` | 普通文本或脱敏文本 | 无，按 `owner_only` / `organization_members` / `conversation_members` 检查 `view` |
| `knowledge_original:{id}` | 同一知识条目的 `original_content_ref` | 不展示 | 敏感文本原文申请 `view` |
| `attachment_meta:{id}` | `knowledge.attachments` 元数据 | 文件名、类型、大小、状态 | 无，按基础访问范围检查 `view` |
| `attachment_content:{id}` | `knowledge.attachments.object_ref` | 不直接展示 | 受保护附件申请 `view` 或 `download` |

非敏感文本的 `content_ref` 与 `original_content_ref` 可以引用同一不可变对象。敏感文本的 `content_ref` 指向脱敏派生对象，`original_content_ref` 仍指向原件。普通 ES 索引只写入 `content_ref` 对应的文本，不写入需审批的原文。`content_ref` 即当前 Contract 对外返回的展示内容引用。

### 1.5 会话数据写入边界

外部平台通常会先返回当前账号可见的群聊 / 私聊列表，用于让用户选择接入目标。这一步只在连接器服务内存或短期 Redis 缓存中处理，不将未选择的会话写入 PostgreSQL，也不创建外部会话总表。

用户确认目标知识库并完成权限校验后，才创建一条 `knowledge.conversation_ingestions`。该表直接保存平台、平台工作区标识、外部会话 ID 和接入元数据，并作为后续数据的根记录：

```text
conversation_ingestions
├── conversation_memberships（仅已接入会话）
├── conversation_collectors
├── messages
│   └── message_sources
├── attachments
└── knowledge_items
```

未接入会话不会产生成员、消息、附件、知识条目或采集检查点记录。接入解除后保留原接入记录及其历史知识，用于权限撤销、审计和重新接入时区分；解除之后不再继续写入新消息。

私聊内容共享采用“按消息批次共享”：用户可以在同一次操作中混合选择一条或多条文字、文件、图片等私聊消息，统一通过 `message_ids` 提交。系统只共享明确选中的消息，不根据时间、上下文或相邻关系自动附带其他消息。文字消息创建组织侧逻辑 `KnowledgeItem`；文件或图片消息创建组织侧逻辑 `KnowledgeItem` 和该消息自身对应的组织逻辑附件。组织侧引用复用私人内容对象，但不改变私人原件的 `owner_only` 权限。未被选择的私聊消息不会进入组织知识库，一期不支持整个私聊持续共享。

消息批次共享使用 `private.conversation.share.requested` 事件；`share_request_id` 作为 API 请求幂等标识，`share_batch_id` 标识本次选择的一组消息，`source_private_item_id` 指向每个被共享的私人消息知识条目。事件处理按 `message_ids` 逐条校验并创建组织逻辑引用：文字消息只创建文字知识引用，文件或图片消息创建知识引用及其自身的组织逻辑附件引用。单条资源处理失败时记录失败结果，批次状态按全部选中消息的处理结果确定。事件完成后，组织逻辑副本的 `source_type` 为 `shared_private_item`，其 `knowledge_scope='organization'`、`access_scope='organization_members'`。共享不创建持续绑定，不影响私聊后续采集，也不会自动共享后续消息。

## 2. `iam` Schema（8 张）

### 2.1 `iam.users`（用户表）

用途：保存系统内部用户主体和基本资料。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 全局用户 ID |
| `email` | VARCHAR(320) | 否 | 否 | 无 | `UNIQUE (lower(email)) WHERE deleted_at IS NULL` | 注册和登录邮箱 |
| `nickname` | VARCHAR(100) | 否 | 否 | 无 | 无 | 用户昵称 |
| `avatar_object_key` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 头像对象键 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `pending`、`active`、`disabled` |
| `email_verified_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 邮箱验证时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |
| `deleted_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 注销或软删除时间 |

### 2.2 `iam.user_credentials`（用户凭证表）

用途：保存密码及后续 OIDC / SSO 登录凭证，不保存明文密码。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 凭证 ID |
| `user_id` | UUID | 否 | 否 | 无 | 外键 → `iam.users.id`；索引 | 所属用户 |
| `credential_type` | VARCHAR(32) | 否 | 否 | `'password'` | CHECK | `password`、`oidc`、`sso` |
| `credential_key` | VARCHAR(320) | 否 | 否 | 无 | `UNIQUE (credential_type, lower(credential_key))` | 邮箱或外部主体标识 |
| `password_hash` | VARCHAR(255) | 否 | 是 | `NULL` | 无 | Argon2id / bcrypt 哈希 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | CHECK | `active`、`disabled` |
| `password_changed_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 最近改密时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

### 2.3 `iam.organizations`（组织表）

用途：保存组织主体；当前 Owner 由成员角色表达。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 组织 ID |
| `name` | VARCHAR(200) | 否 | 否 | 无 | 无 | 组织名称 |
| `slug` | VARCHAR(100) | 否 | 否 | 无 | `UNIQUE (lower(slug))` | 组织可读标识 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`suspended`、`dissolved` |
| `created_by_user_id` | UUID | 否 | 否 | 无 | 外键 → `iam.users.id`；索引 | 最初创建人，仅用于审计 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |
| `dissolved_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 解散时间 |

### 2.4 `iam.organization_memberships`（组织成员关系表）

用途：保存加入、暂停和退出审批生命周期。第二版将原退出申请表合并到本表。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 成员关系 ID |
| `organization_id` | UUID | 否 | 否 | 无 | 外键 → `iam.organizations.id`；联合唯一 | 所属组织 |
| `user_id` | UUID | 否 | 否 | 无 | 外键 → `iam.users.id`；联合唯一 | 成员用户 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`suspended`、`leaving`、`left` |
| `joined_via` | VARCHAR(32) | 否 | 否 | `'created'` | CHECK | `created`、`invitation` |
| `invitation_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.organization_invitations.id` | 邀请来源 |
| `joined_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 最近加入时间 |
| `exit_reason` | TEXT | 否 | 是 | `NULL` | 无 | 当前退出申请理由 |
| `exit_requested_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 进入 `leaving` 的时间 |
| `exit_reviewed_by_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id` | 审批员 |
| `exit_review_note` | TEXT | 否 | 是 | `NULL` | 无 | 审批说明 |
| `exit_reviewed_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 审批时间 |
| `left_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 获批退出时间 |
| `suspended_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 暂停时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 首次创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：`UNIQUE (organization_id, user_id)`；一期增加部分唯一索引 `UNIQUE (user_id) WHERE status IN ('active','suspended','leaving')`，未来开放多组织时删除该索引。再次加入同一组织时恢复原记录并清空上一轮退出审批字段。

### 2.5 `iam.membership_roles`（成员角色表）

用途：支持成员同时具有多个固定角色，以及多 Owner 和 Owner 转让。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 角色关系 ID |
| `membership_id` | UUID | 否 | 否 | 无 | 外键 → `iam.organization_memberships.id`；联合唯一 | 成员关系 |
| `role_code` | VARCHAR(64) | 否 | 否 | 无 | CHECK；联合唯一 | 固定角色代码 |
| `granted_by_user_id` | UUID | 否 | 否 | 无 | 外键 → `iam.users.id` | 授予人 |
| `granted_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 授予时间 |
| `revoked_by_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id` | 撤销人 |
| `revoked_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 为空表示有效 |

关键约束：`UNIQUE (membership_id, role_code) WHERE revoked_at IS NULL`。组织至少有一个有效 Owner 由 IAM 服务在加锁事务中校验。

### 2.6 `iam.organization_invitations`（组织邀请表）

用途：保存 Owner 或审批员创建的一次性邀请链接。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 邀请 ID |
| `organization_id` | UUID | 否 | 否 | 无 | 外键 → `iam.organizations.id`；索引 | 目标组织 |
| `created_by_user_id` | UUID | 否 | 否 | 无 | 外键 → `iam.users.id` | 创建人 |
| `token_hash` | CHAR(64) | 否 | 否 | 无 | 唯一索引 | 邀请 Token SHA-256 |
| `status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `pending`、`accepted`、`revoked`、`expired` |
| `expires_at` | TIMESTAMPTZ | 否 | 否 | 无 | 索引 | 过期时间 |
| `accepted_by_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id` | 接受人 |
| `accepted_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 接受时间 |
| `revoked_by_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id` | 撤销人 |
| `revoked_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 撤销时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |

### 2.7 `iam.access_requests`（受保护资源访问申请与授权表）

用途：保存组织资源的敏感文本原文和受保护附件内容申请、单次审批、授权同步、有效期和撤销；私人资源一期不使用本表。第二版将原审批记录和显式授权表合并到本表。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 申请 ID |
| `organization_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.organizations.id`；索引 | 组织资源所属组织；私人资源为空 |
| `requester_user_id` | UUID | 否 | 否 | 无 | 外键 → `iam.users.id`；索引 | 申请人 |
| `resource_scope` | VARCHAR(16) | 否 | 否 | 无 | CHECK；索引 | `private`、`organization`；一期仅处理 `organization` |
| `resource_type` | VARCHAR(64) | 否 | 否 | 无 | 联合索引；CHECK | `knowledge_original`、`attachment_content` |
| `resource_id` | UUID | 否 | 否 | 无 | 联合索引 | 模块二资源逻辑 ID |
| `action` | VARCHAR(16) | 否 | 否 | 无 | CHECK | `view`、`download`；原文只允许 `view` |
| `reason` | TEXT | 否 | 是 | `NULL` | 无 | 申请理由 |
| `status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `pending`、`approved`、`rejected`、`cancelled`、`expired`、`revoked` |
| `request_expires_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 待审批截止时间 |
| `reviewed_by_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id` | 信息管理员或该会话采集者 |
| `reviewer_basis` | VARCHAR(32) | 否 | 是 | `NULL` | CHECK | `information_admin`、`collector` |
| `review_note` | TEXT | 否 | 是 | `NULL` | 无 | 审批说明 |
| `reviewed_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 审批时间 |
| `grant_expires_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 授权失效时间；为空表示长期 |
| `fga_sync_status` | VARCHAR(32) | 否 | 否 | `'not_started'` | 索引；CHECK | `not_started`、`pending`、`synced`、`failed`、`removed` |
| `fga_tuple_key` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | OpenFGA 关系的幂等键或追踪键 |
| `granted_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 关系同步成功时间 |
| `revoked_by_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id` | 撤销人 |
| `revoked_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 撤销或过期清理时间 |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近同步错误 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：同一用户、资源和动作只允许一个 `pending` 申请；`resource_scope='private'` 时 `organization_id` 必须为空，`resource_scope='organization'` 时 `organization_id` 必须非空。一期申请接口只接受组织资源，私人资源由所有者直接访问，不创建申请记录。只在 `status='approved' AND fga_sync_status='synced'` 且授权未过期时视为显式授权。数据库记录负责审批和生命周期，OpenFGA tuple 负责在线鉴权。

### 2.8 `iam.audit_logs`（IAM 审计日志表）

用途：只追加记录登录、成员、角色、访问检查、审批、原文查看和附件下载等安全事件。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 审计 ID |
| `actor_user_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.users.id`；索引 | 系统任务可为空 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 外键 → `iam.organizations.id`；索引 | 组织上下文 |
| `action` | VARCHAR(100) | 否 | 否 | 无 | 索引 | 如 `original.viewed`、`attachment.downloaded` |
| `resource_type` | VARCHAR(64) | 否 | 是 | `NULL` | 联合索引 | 资源类型 |
| `resource_id` | UUID | 否 | 是 | `NULL` | 联合索引 | 跨服务逻辑 ID |
| `result` | VARCHAR(32) | 否 | 否 | `'success'` | CHECK | `success`、`failed`、`denied` |
| `request_id` | VARCHAR(100) | 否 | 是 | `NULL` | 索引 | 链路请求 ID |
| `ip_address` | INET | 否 | 是 | `NULL` | 无 | 客户端 IP |
| `detail` | JSONB | 否 | 否 | `'{}'::jsonb` | GIN 索引按需增加 | 禁止写入正文、密码和 Token |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 审计时间 |

## 3. `knowledge` Schema（11 张）

### 3.1 `knowledge.knowledge_bases`（知识库表）

用途：表示私人会话库、私人本地库、组织群聊库和公司文件库，不再单独建立知识空间表。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 知识库 ID |
| `knowledge_scope` | VARCHAR(16) | 否 | 否 | 无 | CHECK | `private`、`organization` |
| `base_type` | VARCHAR(32) | 否 | 否 | 无 | CHECK | `private_conversation`、`private_local`、`organization_conversation`、`organization_files` |
| `name` | VARCHAR(200) | 否 | 否 | 无 | 无 | 展示名称 |
| `owner_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 私人库的 `iam.users.id` 逻辑引用 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织库的 `iam.organizations.id` 逻辑引用 |
| `source_key` | VARCHAR(255) | 否 | 是 | `NULL` | 联合唯一 | 会话接入 ID 等稳定来源键 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`archived` |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

约束：`private` 必须有 `owner_user_id` 且 `organization_id IS NULL`；`organization` 反之。每名用户一个 `private_local`，每个组织一个 `organization_files`。

### 3.2 `knowledge.connector_accounts`（个人连接器账号表）

用途：保存内部用户绑定的飞书、企业微信、个人微信等外部账号。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 连接器账号 ID |
| `owner_user_id` | UUID | 否 | 否 | 无 | 索引 | `iam.users.id` 逻辑引用 |
| `platform` | VARCHAR(32) | 否 | 否 | 无 | CHECK；联合唯一 | `feishu`、`wecom`、`wechat` |
| `platform_workspace_key` | VARCHAR(255) | 否 | 否 | `''` | 联合唯一 | 飞书 / 企业微信企业或平台工作区标识；不是系统租户 ID |
| `external_account_id` | VARCHAR(255) | 否 | 否 | 无 | 联合唯一 | 外部账号 ID |
| `display_name` | VARCHAR(200) | 否 | 是 | `NULL` | 无 | 外部账号名称 |
| `credential_ref` | VARCHAR(512) | 否 | 否 | 无 | 无 | KMS / Secret Manager 引用，不保存明文 Token |
| `token_expires_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | Token 过期时间 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`expired`、`revoked`、`error` |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近连接错误 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：`UNIQUE (platform, platform_workspace_key, external_account_id)`，保证一个外部账号不被重复绑定。

### 3.3 `knowledge.external_identities`（外部身份表）

用途：保存已经接入会话中出现的外部联系人、消息发送人或群成员，并可选映射到内部用户。未接入会话中的外部身份不落库。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 外部身份 ID |
| `platform` | VARCHAR(32) | 否 | 否 | 无 | CHECK；联合唯一 | 平台 |
| `platform_workspace_key` | VARCHAR(255) | 否 | 否 | `''` | 联合唯一 | 外部平台企业 / 工作区标识，不是系统租户 ID |
| `external_user_id` | VARCHAR(255) | 否 | 否 | 无 | 联合唯一 | 平台用户 ID |
| `display_name` | VARCHAR(200) | 否 | 是 | `NULL` | 无 | 平台显示名 |
| `avatar_url` | TEXT | 否 | 是 | `NULL` | 无 | 外部头像地址 |
| `mapped_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 可选的 `iam.users.id` 逻辑引用 |
| `mapping_status` | VARCHAR(32) | 否 | 否 | `'unmapped'` | 索引；CHECK | `unmapped`、`mapped`、`conflict` |
| `mapped_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 映射时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：`UNIQUE (platform, platform_workspace_key, external_user_id)`。

### 3.4 `knowledge.conversation_memberships`（已接入会话成员有效期表）

用途：只为已经创建接入记录的会话维护平台成员动态有效期，作为 OpenFGA 会话用户组的事实来源。未接入会话不会创建本表记录。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 会话成员关系 ID |
| `conversation_ingestion_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.conversation_ingestions.id`；联合唯一 | 已接入会话 |
| `external_identity_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.external_identities.id`；联合唯一 | 外部成员 |
| `member_role` | VARCHAR(32) | 否 | 是 | `NULL` | 无 | 平台群主、管理员、成员等 |
| `status` | VARCHAR(16) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`left` |
| `joined_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 平台可提供时保存 |
| `left_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 退出时间 |
| `last_seen_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 最近同步确认时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：`UNIQUE (conversation_ingestion_id, external_identity_id)`。同步后只将“外部身份已映射到内部用户、内部用户仍属于接入组织、群成员有效”的交集写入 OpenFGA 用户组。

### 3.5 `knowledge.conversation_ingestions`（会话接入表）

用途：保存用户确认后的私人会话采集或群聊组织接入，是会话相关数据的根记录。平台会话发现结果在内存或短期缓存中处理，只有接入成功后才写入本表。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 接入 ID |
| `platform` | VARCHAR(32) | 否 | 否 | 无 | CHECK；联合唯一 | `feishu`、`wecom`、`wechat` |
| `platform_workspace_key` | VARCHAR(255) | 否 | 否 | `''` | 联合唯一 | 外部平台企业 / 工作区标识，不是系统租户 ID |
| `external_conversation_id` | VARCHAR(255) | 否 | 否 | 无 | 联合唯一 | 外部会话 ID；仅接入后保存 |
| `conversation_type` | VARCHAR(16) | 否 | 否 | 无 | CHECK | `private`、`group` |
| `name` | VARCHAR(300) | 否 | 是 | `NULL` | 无 | 接入时获取的会话名称 |
| `avatar_url` | TEXT | 否 | 是 | `NULL` | 无 | 接入时获取的群头像 |
| `platform_metadata` | JSONB | 否 | 否 | `'{}'::jsonb` | GIN 按需 | 少量平台扩展字段 |
| `last_synced_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 已接入会话元数据最近同步时间 |
| `knowledge_base_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.knowledge_bases.id`；索引 | 目标知识库 |
| `ingestion_scope` | VARCHAR(16) | 否 | 否 | 无 | CHECK | `private`、`organization` |
| `owner_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 私人接入所有者 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织接入的组织 ID |
| `created_by_user_id` | UUID | 否 | 否 | 无 | 索引 | 发起接入的内部用户 |
| `requested_start_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 用户选择的采集起点 |
| `effective_start_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 平台实际可采集起点 |
| `permission_group_key` | VARCHAR(255) | 否 | 是 | `NULL` | 唯一索引 | 组织群聊对应的 OpenFGA group key |
| `acl_version` | BIGINT | 否 | 否 | `0` | CHECK `>= 0` | 权限事实版本 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`paused`、`detached`、`error` |
| `pause_reason` | VARCHAR(100) | 否 | 是 | `NULL` | 无 | 如 `no_available_collector` |
| `detached_by_user_id` | UUID | 否 | 是 | `NULL` | 无 | 解除人 |
| `detached_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 解除时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

约束：私人接入必须有 `owner_user_id` 且组织为空；组织接入反之。群聊增加部分唯一索引 `UNIQUE (platform, platform_workspace_key, external_conversation_id) WHERE ingestion_scope='organization' AND status IN ('active','paused')`，禁止同一群聊重复绑定组织；私人接入增加 `UNIQUE (platform, platform_workspace_key, external_conversation_id, owner_user_id) WHERE ingestion_scope='private' AND status IN ('active','paused')`，防止同一用户重复接入同一私聊。解除后可新建接入。

### 3.6 `knowledge.conversation_collectors`（会话采集者与检查点表）

用途：保存主 / 补充采集者、个人连接器以及各自采集游标。第二版将采集检查点合并到本表。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 采集者 ID |
| `conversation_ingestion_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.conversation_ingestions.id`；联合唯一 | 所属接入 |
| `connector_account_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.connector_accounts.id`；联合唯一 | 使用的个人连接器 |
| `collector_user_id` | UUID | 否 | 否 | 无 | 索引 | 连接器所属内部用户快照 |
| `collector_role` | VARCHAR(16) | 否 | 否 | 无 | CHECK | `primary`、`supplemental` |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`unavailable`、`removed` |
| `last_cursor` | TEXT | 否 | 是 | `NULL` | 无 | 平台游标；无游标的平台可为空 |
| `last_success_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 最近成功采集时间 |
| `last_attempt_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 最近尝试时间 |
| `next_poll_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 组合索引 | 下次轮询时间 |
| `consecutive_failures` | INTEGER | 否 | 否 | `0` | CHECK `>= 0` | 连续失败次数 |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近错误 |
| `joined_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 成为采集者时间 |
| `removed_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 移除时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：`UNIQUE (conversation_ingestion_id, connector_account_id) WHERE status <> 'removed'`；每个接入 `UNIQUE (conversation_ingestion_id) WHERE collector_role='primary' AND status='active'`。只有消息及其 Outbox 在事务中成功后才推进检查点。

### 3.7 `knowledge.messages`（统一消息表）

用途：保存去重后的统一消息及来源上下文，不保存多采集者的重复正文。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 内部消息 ID |
| `conversation_ingestion_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.conversation_ingestions.id`；联合唯一 | 接入上下文 |
| `external_message_id` | VARCHAR(255) | 否 | 否 | 无 | 联合唯一 | 外部消息 ID |
| `sender_identity_id` | UUID | 否 | 是 | `NULL` | 外键 → `knowledge.external_identities.id`；索引 | 外部发送人 |
| `message_type` | VARCHAR(32) | 否 | 否 | 无 | CHECK | `text`、`image`、`file`、`mixed`、`system` |
| `normalized_content_ref` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 规范化原始文本对象引用 |
| `content_hash` | CHAR(64) | 否 | 否 | 无 | 索引 | 统一消息内容 SHA-256 |
| `content_version` | INTEGER | 否 | 否 | `1` | CHECK `>= 1` | 一期固定 1 |
| `sent_at` | TIMESTAMPTZ | 否 | 否 | 无 | 组合索引 | 平台发送时间 |
| `platform_updated_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 为后续编辑预留 |
| `lifecycle_status` | VARCHAR(16) | 否 | 否 | `'active'` | CHECK | 一期仅 `active` |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 首次采集时间 |

关键约束：`UNIQUE (conversation_ingestion_id, external_message_id)`。同一外部群聊解除并重新接入其他组织后，新的接入可以形成独立消息记录。

### 3.8 `knowledge.message_sources`（消息多采集来源表）

用途：记录每个采集者对同一消息的观测，支持补充采集、去重、追踪和原始快照审计。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 来源记录 ID |
| `message_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.messages.id`；联合唯一 | 统一消息 |
| `collector_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.conversation_collectors.id`；联合唯一 | 采集者 |
| `external_message_id` | VARCHAR(255) | 否 | 否 | 无 | 联合唯一 | 采集侧外部消息 ID |
| `raw_payload_ref` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 可选的原始响应对象引用 |
| `payload_hash` | CHAR(64) | 否 | 否 | 无 | 无 | 原始载荷哈希 |
| `collected_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 采集时间 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |

关键约束：`UNIQUE (collector_id, external_message_id)` 和 `UNIQUE (message_id, collector_id)`。本表同时承担初版 `message_observations` 的精简职责。

### 3.9 `knowledge.attachments`（附件表）

用途：保存群聊附件和本地上传文件的元数据、对象引用、提取文本及内容保护状态。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 附件 ID |
| `request_id` | VARCHAR(100) | 否 | 是 | `NULL` | 本地上传部分唯一索引 | 本地上传请求幂等键；平台附件为空并使用来源组合键幂等 |
| `message_id` | UUID | 否 | 是 | `NULL` | 外键 → `knowledge.messages.id`；索引 | 群聊 / 私聊附件来源 |
| `uploaded_by_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 本地上传人 |
| `upload_destination` | VARCHAR(32) | 否 | 是 | `NULL` | CHECK | `private_local_library`、`organization_file_library` |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织附件归属 |
| `file_name` | VARCHAR(500) | 否 | 否 | 无 | 无 | 文件名 |
| `mime_type` | VARCHAR(255) | 否 | 否 | 无 | 索引 | MIME 类型 |
| `size_bytes` | BIGINT | 否 | 否 | 无 | CHECK `>= 0` | 文件大小 |
| `object_ref` | VARCHAR(512) | 否 | 否 | 无 | 唯一索引 | 原附件对象引用 |
| `source_private_attachment_id` | UUID | 否 | 是 | `NULL` | 索引 | 共享私人文件时指向私人附件；普通附件为空 |
| `content_hash` | CHAR(64) | 否 | 否 | 无 | 索引 | 文件 SHA-256 |
| `content_version` | INTEGER | 否 | 否 | `1` | CHECK `>= 1` | 一期固定 1 |
| `metadata_access_scope` | VARCHAR(32) | 否 | 否 | 无 | CHECK | 与所属知识的基础范围一致 |
| `content_access_scope` | VARCHAR(32) | 否 | 否 | 无 | CHECK | 基础范围；受保护时还需显式授权 |
| `acl_version` | BIGINT | 否 | 否 | `0` | CHECK `>= 0` | 附件资源权限版本 |
| `encrypted` | BOOLEAN | 否 | 否 | `FALSE` | 无 | 对象是否加密保存 |
| `content_access_required` | BOOLEAN | 否 | 否 | `FALSE` | 索引 | 是否必须申请附件内容权限 |
| `upload_status` | VARCHAR(16) | 否 | 否 | `'pending'` | 索引；CHECK | `pending`、`validating`、`uploading`、`uploaded`、`failed`、`duplicate` |
| `upload_error` | TEXT | 否 | 是 | `NULL` | 无 | 上传失败摘要 |
| `uploaded_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 对象存储写入完成时间 |
| `extracted_original_ref` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 原始解析文本引用，不进入普通索引 |
| `extracted_display_ref` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 可索引的普通 / 脱敏解析文本 |
| `processing_status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `pending`、`processing`、`ready`、`failed` |
| `sensitivity` | VARCHAR(32) | 否 | 是 | `NULL` | 索引；CHECK | `public`、`internal`、`confidential`、`restricted` |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

约束：`message_id` 与 `uploaded_by_user_id` 至少一个非空；本地上传必须填写 `upload_destination`、`request_id` 和 `upload_status`；平台附件的 `upload_destination` 为空且上传完成后置为 `uploaded`。本地上传使用 `UNIQUE (request_id) WHERE upload_destination IS NOT NULL` 幂等；平台附件不使用全局 `request_id`，使用 `message_id + 外部附件 ID` 或采集来源组合键幂等。校验失败、对象存储失败写入 `upload_status='failed'` 和 `upload_error`；content_hash 已存在且复用已有对象时写入 `upload_status='duplicate'`。`source_private_attachment_id` 为本表自引用逻辑外键，只有共享私人文件时填写，并创建新的组织逻辑附件 ID。文件库是聚合视图，群聊附件仍保持 `conversation_members`，不能因为出现在公司文件库而扩大权限。

### 3.10 `knowledge.knowledge_items`（知识条目表）

用途：作为模块二向 RAG 和 IAM 暴露的稳定知识资源，合并内容版本、隐私任务状态和 ACL 同步状态。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | `knowledge_item` 资源 ID |
| `knowledge_base_id` | UUID | 否 | 否 | 无 | 外键 → `knowledge.knowledge_bases.id`；索引 | 所属知识库 |
| `knowledge_scope` | VARCHAR(16) | 否 | 否 | 无 | CHECK；索引 | `private`、`organization` |
| `access_scope` | VARCHAR(32) | 否 | 否 | 无 | CHECK；索引 | `owner_only`、`organization_members`、`conversation_members` |
| `owner_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 私人知识所有者 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织知识归属 |
| `conversation_ingestion_id` | UUID | 否 | 是 | `NULL` | 外键 → `knowledge.conversation_ingestions.id`；索引 | 群聊权限来源 |
| `source_type` | VARCHAR(32) | 否 | 否 | 无 | CHECK；索引 | `platform_conversation`、`private_conversation`、`local_upload`、`shared_private_item` |
| `source_message_id` | UUID | 否 | 是 | `NULL` | 外键 → `knowledge.messages.id`；索引 | 消息来源 |
| `source_attachment_id` | UUID | 否 | 是 | `NULL` | 外键 → `knowledge.attachments.id`；索引 | 附件来源 |
| `source_private_item_id` | UUID | 否 | 是 | `NULL` | 外键 → `knowledge.knowledge_items.id`；索引 | 共享私人知识的来源 |
| `share_request_id` | VARCHAR(100) | 否 | 是 | `NULL` | 索引 | 单条消息 / 文件共享请求幂等键；对应 Contract `sharing.share_request_id` |
| `share_batch_id` | UUID | 否 | 是 | `NULL` | 索引 | 同一次选择多条消息 / 文件时的批次键，不单独建表 |
| `shared_by_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 共享人 |
| `shared_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 共享时间 |
| `content_type` | VARCHAR(32) | 否 | 否 | 无 | CHECK | `text`、`file`、`image`、`spreadsheet`、`mixed` |
| `content_ref` | VARCHAR(512) | 否 | 否 | 无 | 无 | 对外 Contract 的展示内容引用；普通或脱敏文本，等价于展示内容引用 |
| `original_content_ref` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 受保护原文；文件可引用附件对象 |
| `content_hash` | CHAR(64) | 否 | 否 | 无 | 索引 | 当前业务内容哈希 |
| `content_version` | INTEGER | 否 | 否 | `1` | CHECK `>= 1` | 业务内容版本，一期固定 1 |
| `content_visibility` | VARCHAR(16) | 否 | 否 | `'original'` | CHECK | `original`、`masked`、`metadata_only` |
| `original_access_required` | BOOLEAN | 否 | 否 | `FALSE` | 索引 | 查看原文是否需要显式授权 |
| `security_status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `not_required`、`pending`、`processing`、`classified`、`failed` |
| `sensitivity` | VARCHAR(32) | 否 | 是 | `NULL` | 索引；CHECK | `public`、`internal`、`confidential`、`restricted` |
| `security_policy_version` | VARCHAR(64) | 否 | 是 | `NULL` | 无 | 脱敏 / 分类策略版本 |
| `content_saved` | BOOLEAN | 否 | 否 | `FALSE` | 无 | 就绪闸门 |
| `ownership_ready` | BOOLEAN | 否 | 否 | `FALSE` | 无 | 就绪闸门 |
| `security_ready` | BOOLEAN | 否 | 否 | `FALSE` | 无 | 就绪闸门 |
| `permission_ready` | BOOLEAN | 否 | 否 | `FALSE` | 无 | OpenFGA 关系已准备 |
| `acl_version` | BIGINT | 否 | 否 | `0` | CHECK `>= 0` | 资源权限关系版本 |
| `acl_sync_status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `not_required`、`pending`、`synced`、`failed` |
| `processing_status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `pending`、`published`、`processing`、`ready`、`failed` |
| `lifecycle_status` | VARCHAR(16) | 否 | 否 | `'active'` | 索引；CHECK | 一期仅 `active` |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近隐私、ACL 或发布错误 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

约束：私人知识必须 `owner_user_id NOT NULL AND organization_id IS NULL AND access_scope='owner_only'`；组织知识反之。来源类型约束为：`local_upload` 必须有 `source_attachment_id`；`private_conversation` 必须有 `source_message_id`；`shared_private_item` 必须有 `source_private_item_id`、`share_request_id`、`shared_by_user_id` 和 `shared_at`；`platform_conversation` 必须有 `conversation_ingestion_id`，且其 `access_scope='conversation_members'`。这些关联存在性可用行级 CHECK 约束，跨记录归属和所有权由模块二在事务中校验。只有四个就绪字段全为真时才允许登记唯一的 `knowledge.ready` 事件。共享文字消息时只创建组织知识引用；共享文件或图片消息时创建组织知识引用及该消息自身的组织逻辑附件引用。普通内容复用不可变对象和解析结果，只有脱敏派生内容新建对象。未选择共享的私人消息不会产生组织副本。
共享幂等约束：`UNIQUE (share_request_id, source_private_item_id)`，允许同一请求共享多条不同私人内容，但同一私人内容不会重复创建组织副本。

### 3.11 `knowledge.outbox_events`（知识服务事务事件表）

用途：可靠发布隐私识别、附件解析、知识就绪和处理结果事件，避免消息提交后任务丢失。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 同时作为 `event_id` |
| `aggregate_type` | VARCHAR(64) | 否 | 否 | 无 | 联合索引 | 聚合类型 |
| `aggregate_id` | UUID | 否 | 否 | 无 | 联合索引 | 聚合 ID |
| `event_type` | VARCHAR(100) | 否 | 否 | 无 | 索引 | Contract 中定义的事件类型 |
| `event_version` | BIGINT | 否 | 否 | `1` | 联合唯一 | 同一聚合事件版本 |
| `schema_version` | INTEGER | 否 | 否 | `1` | CHECK `>= 1` | 事件载荷版本 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 私人知识为空 |
| `trace_id` | VARCHAR(100) | 否 | 否 | 无 | 索引 | 链路 ID |
| `payload` | JSONB | 否 | 否 | `'{}'::jsonb` | 无 | 稳定跨服务载荷，不放大正文或二进制 |
| `status` | VARCHAR(32) | 否 | 否 | `'pending'` | 组合索引；CHECK | `pending`、`publishing`、`published`、`failed` |
| `retry_count` | INTEGER | 否 | 否 | `0` | CHECK `>= 0` | 重试次数 |
| `available_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 组合索引 | 下次可发布时间 |
| `published_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | Redis Streams 发布成功时间 |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近错误 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 创建时间 |

关键约束：`UNIQUE (aggregate_type, aggregate_id, event_type, event_version)`。保存消息 / 知识项与写入任务事件必须在同一事务中完成。

## 4. `rag` Schema（5 张）

### 4.1 `rag.processing_jobs`（处理任务表）

用途：统一承载事件消费幂等、附件预解析、切块、Embedding、重建索引和 ACL 刷新，并保存解析产物引用。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 任务 ID |
| `source_event_id` | UUID | 否 | 否 | 无 | 唯一索引 | 至少一次投递的幂等键 |
| `event_type` | VARCHAR(100) | 否 | 否 | 无 | 索引 | 来源事件类型 |
| `payload_hash` | CHAR(64) | 否 | 否 | 无 | 无 | 相同事件 ID 载荷一致性校验 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织上下文 |
| `knowledge_item_id` | UUID | 否 | 否 | 无 | 联合索引 | 模块二知识条目逻辑 ID |
| `content_version` | INTEGER | 否 | 否 | 无 | 联合索引 | 指定业务内容版本 |
| `acl_version` | BIGINT | 否 | 否 | 无 | 索引 | 指定权限版本 |
| `job_type` | VARCHAR(32) | 否 | 否 | 无 | CHECK；索引 | `preparse`、`full_process`、`reindex`、`acl_refresh`、`delete_index` |
| `status` | VARCHAR(32) | 否 | 否 | `'pending'` | 组合索引；CHECK | `pending`、`processing`、`succeeded`、`failed`、`dead` |
| `current_stage` | VARCHAR(32) | 否 | 是 | `NULL` | 索引 | `fetch`、`parse`、`chunk`、`embed`、`index` |
| `parser_name` | VARCHAR(64) | 否 | 是 | `NULL` | 无 | 如 `mineru`、`plain_text` |
| `parser_version` | VARCHAR(64) | 否 | 是 | `NULL` | 无 | 解析器版本 |
| `parsed_artifact_ref` | VARCHAR(512) | 否 | 是 | `NULL` | 无 | 解析产物对象引用 |
| `parsed_content_hash` | CHAR(64) | 否 | 是 | `NULL` | 无 | 解析产物哈希 |
| `page_count` | INTEGER | 否 | 是 | `NULL` | CHECK `>= 0` | 页数 |
| `chunking_version` | VARCHAR(64) | 否 | 是 | `NULL` | 无 | 切块配置版本 |
| `embedding_model` | VARCHAR(128) | 否 | 是 | `NULL` | 无 | Embedding 模型及版本 |
| `retry_count` | INTEGER | 否 | 否 | `0` | CHECK `>= 0` | 重试次数 |
| `available_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 组合索引 | 下次执行时间 |
| `started_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 开始时间 |
| `finished_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 无 | 完成时间 |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近错误 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 索引 | 创建时间 |

关键约束：`source_event_id` 全局唯一；同一 `knowledge_item_id + content_version + job_type` 只允许一个未终结任务。旧内容版本或旧 ACL 任务不得覆盖新版本。

### 4.2 `rag.index_records`（ES 索引记录表）

用途：记录知识版本在 Elasticsearch 的索引状态；PostgreSQL 不重复保存全部切块文本和向量。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 索引记录 ID |
| `knowledge_item_id` | UUID | 否 | 否 | 无 | 联合唯一 | 模块二知识条目逻辑 ID |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织过滤字段 |
| `owner_user_id` | UUID | 否 | 是 | `NULL` | 索引 | 私人知识过滤字段 |
| `content_version` | INTEGER | 否 | 否 | 无 | 联合唯一 | 已索引内容版本 |
| `acl_version` | BIGINT | 否 | 否 | 无 | 索引 | 已索引 ACL 版本 |
| `content_variant` | VARCHAR(16) | 否 | 否 | `'display'` | CHECK | 一期只允许 `display`，禁止原文索引 |
| `es_index_alias` | VARCHAR(255) | 否 | 否 | `'knowledge_chunks_read'` | 索引 | 读别名 |
| `es_document_prefix` | VARCHAR(255) | 否 | 否 | 无 | 索引 | 切块文档 ID 前缀 |
| `chunk_count` | INTEGER | 否 | 否 | `0` | CHECK `>= 0` | 切块数 |
| `mapping_version` | VARCHAR(32) | 否 | 否 | `'v1'` | 索引 | ES Mapping / 分词 / 向量维度版本 |
| `status` | VARCHAR(32) | 否 | 否 | `'pending'` | 索引；CHECK | `pending`、`indexing`、`ready`、`failed`、`deleted` |
| `indexed_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 完成时间 |
| `last_error` | TEXT | 否 | 是 | `NULL` | 无 | 最近错误 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |

关键约束：`UNIQUE (knowledge_item_id, content_version, content_variant)`；一期 `content_variant` 固定为 `display`。

### 4.3 `rag.search_history`（搜索历史表）

用途：保存搜索关键词、过滤条件和性能统计，不保存完整候选正文。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | 搜索记录 ID |
| `user_id` | UUID | 否 | 否 | 无 | 组合索引 | `iam.users.id` 逻辑引用 |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织上下文，私人搜索为空 |
| `query_text` | TEXT | 否 | 否 | 无 | 无 | 搜索词；按保留策略清理 |
| `query_hash` | CHAR(64) | 否 | 否 | 无 | 索引 | 归一化查询哈希 |
| `filters` | JSONB | 否 | 否 | `'{}'::jsonb` | 无 | 知识库、来源、时间等过滤条件 |
| `result_count` | INTEGER | 否 | 否 | `0` | CHECK `>= 0` | 权限过滤后的结果数 |
| `duration_ms` | INTEGER | 否 | 是 | `NULL` | CHECK `>= 0` | 总耗时 |
| `request_id` | VARCHAR(100) | 否 | 是 | `NULL` | 索引 | 链路请求 ID |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | `(user_id, created_at DESC)` | 搜索时间 |

### 4.4 `rag.qa_conversations`（问答会话表）

用途：保存用户 RAG 问答会话。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | QA 会话 ID |
| `user_id` | UUID | 否 | 否 | 无 | 组合索引 | 用户逻辑 ID |
| `organization_id` | UUID | 否 | 是 | `NULL` | 索引 | 组织上下文 |
| `title` | VARCHAR(300) | 否 | 是 | `NULL` | 无 | 会话标题 |
| `status` | VARCHAR(32) | 否 | 否 | `'active'` | 索引；CHECK | `active`、`archived`、`deleted` |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | `(user_id, created_at DESC)` | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | 无 | 更新时间 |
| `deleted_at` | TIMESTAMPTZ | 否 | 是 | `NULL` | 索引 | 软删除时间 |

### 4.5 `rag.qa_messages`（问答消息与引用表）

用途：保存问题、回答、生成元数据及当次引用。第二版暂将引用合并为 JSONB；若后续需要复杂引用统计，再拆出引用表。

| 字段名 | 字段类型 | 主键 | 可空 | 默认值 | 索引 / 约束 | 注释 |
|---|---|---:|---:|---|---|---|
| `id` | UUID | 是 | 否 | `gen_random_uuid()` | 主键 | QA 消息 ID |
| `conversation_id` | UUID | 否 | 否 | 无 | 外键 → `rag.qa_conversations.id`；组合索引 | 所属会话 |
| `parent_message_id` | UUID | 否 | 是 | `NULL` | 外键 → `rag.qa_messages.id` | 支持重新生成 |
| `role` | VARCHAR(16) | 否 | 否 | 无 | CHECK | `user`、`assistant`、`system` |
| `content` | TEXT | 否 | 否 | 无 | 无 | 问题或回答正文，只能包含获准展示内容 |
| `citations` | JSONB | 否 | 否 | `'[]'::jsonb` | GIN 按需 | Assistant 引用数组 |
| `model_name` | VARCHAR(128) | 否 | 是 | `NULL` | 索引 | 模型名称和版本 |
| `prompt_version` | VARCHAR(64) | 否 | 是 | `NULL` | 索引 | Prompt 版本 |
| `status` | VARCHAR(32) | 否 | 否 | `'completed'` | 索引；CHECK | `streaming`、`completed`、`failed`、`cancelled` |
| `token_usage` | JSONB | 否 | 否 | `'{}'::jsonb` | 无 | Token 统计 |
| `duration_ms` | INTEGER | 否 | 是 | `NULL` | CHECK `>= 0` | 生成耗时 |
| `error_message` | TEXT | 否 | 是 | `NULL` | 无 | 失败摘要 |
| `created_at` | TIMESTAMPTZ | 否 | 否 | `CURRENT_TIMESTAMP` | `(conversation_id, created_at)` | 创建时间 |

`citations` 每项至少保存 `knowledge_item_id`、`content_version`、`acl_version`、`es_chunk_id`、`rank`、`score` 和最小必要的 `quote_text`。再次展示引用或打开来源时仍须调用 IAM 检查当前 `view` 权限。

## 5. Elasticsearch 初版设计

### 5.1 索引数量与职责

一期只建立一个逻辑索引：

```text
knowledge_chunks_v1
```

生产使用读写别名，例如 `knowledge_chunks_read` 和 `knowledge_chunks_write`。Mapping、分词器或向量维度发生不兼容变化时创建 `knowledge_chunks_v2`，重建完成后原子切换别名。不要按组织创建物理索引，组织和个人隔离通过字段过滤及最终 IAM Check 完成。

### 5.2 ES 文档字段

每个 chunk 一条 ES 文档，保存可全文检索的展示文本、向量和过滤元数据：

| 字段 | 作用 |
|---|---|
| `chunk_id`、`chunk_index` | 切块标识和顺序 |
| `knowledge_item_id`、`content_version`、`acl_version` | 回源、版本一致性和权限复核 |
| `organization_id`、`owner_user_id` | 组织 / 私人范围初筛 |
| `knowledge_scope`、`access_scope` | 基础访问范围初筛 |
| `knowledge_base_id`、`conversation_ingestion_id` | 知识库和群聊来源过滤 |
| `title`、`content` | BM25 全文检索；只存普通或脱敏文本，对应 `knowledge_items.content_ref` |
| `embedding` | 向量召回 |
| `content_hash`、`content_variant` | 一致性校验；一期 `content_variant=display`，对应 `content_ref` |
| `source_type`、`platform`、`sender_identity_id`、`sent_at` | 来源展示和条件过滤 |
| `sensitivity`、`mime_type`、`file_name` | 安全、附件及展示元数据 |

Elasticsearch 不能只存向量和元数据，否则无法进行 BM25 与混合检索。受保护文本原文、附件二进制及仅审批后可看的原始解析文本均不得进入普通索引。

## 6. OpenFGA 初版模型说明

权限动作只保留 `view` 和 `download`。基础资源通过 owner、组织成员或动态群聊用户组取得权限；敏感原文与附件内容通过审批后给用户写显式关系。

```text
type user

type organization
  relations
    define member: [user]

type group
  relations
    define member: [user]

type knowledge_item
  relations
    define owner: [user]
    define organization: [organization]
    define conversation_group: [group]
    define viewer: [user]
    define downloader: [user]
    define view: owner or viewer or member from organization or member from conversation_group
    define download: owner or downloader or member from organization or member from conversation_group

type knowledge_original
  relations
    define parent: [knowledge_item]
    define viewer: [user]
    define view: viewer

type attachment_meta
  relations
    define owner: [user]
    define organization: [organization]
    define conversation_group: [group]
    define viewer: [user]
    define view: owner or viewer or member from organization or member from conversation_group

type attachment_content
  relations
    define parent: [attachment_meta]
    define viewer: [user]
    define downloader: [user]
    define view: viewer
    define download: downloader
```

这是业务语义草案，最终 DSL 需用 OpenFGA CLI 验证。所有基础范围默认同时授予 `view` 和 `download`：私人资源向 owner 写入两种关系；组织本地上传、共享私聊资源向组织成员继承两种关系；群聊资源向动态会话用户组继承两种关系。受保护附件只在审批通过后写用户级 `viewer/downloader` tuple。敏感原文通常只授予 `knowledge_original.view`，不默认提供下载动作。信息管理员和该会话采集者拥有审批资格，不等于天然拥有原文查看或附件下载权限；Owner 如需审批，应同时具备信息管理员角色。

## 7. 关键事务与幂等

1. 接受邀请：锁定邀请、校验用户没有其他有效组织、创建或恢复成员关系并赋默认角色，应在一个 IAM 事务内完成。
2. 批准退出：锁定成员与 Owner 集合、保证组织仍有 Owner、更新成员状态并撤销角色和相关 OpenFGA 关系。
3. 审批组织资源访问：锁定申请、校验审批人当前资格、落审批结果；私人资源一期由所有者直接访问，不创建申请记录。OpenFGA 同步失败时保持 `fga_sync_status='failed'` 并重试，不能误判为已授权。
4. 保存消息：统一消息、消息来源、知识条目以及 `privacy.scan.requested` Outbox 在同一事务提交后，才能推进对应采集者检查点。
5. 私人消息批次共享：校验所有者、组织成员资格以及全部 `message_ids` 均属于该私聊，以 `share_request_id` 保证请求幂等，并按 `share_batch_id + source_private_item_id` 创建组织逻辑引用；一次请求可混合选择文字、文件和图片消息，未选中的消息不得创建组织引用。
6. 本地上传：先以 `attachments.request_id` 幂等创建记录，状态按 `pending → validating → uploading → uploaded` 流转；校验失败或对象存储失败为 `failed`，哈希复用已有对象为 `duplicate`。只有 `uploaded` 后才发布 `document.processing.requested`。
7. 多采集者去重：先用 `collector_id + external_message_id` 去重来源，再用 `conversation_ingestion_id + external_message_id` 合并统一消息。
8. 发布知识：只有 `content_saved && ownership_ready && security_ready && permission_ready` 时才登记 `knowledge.ready`。
9. RAG 消费：以 `processing_jobs.source_event_id` 唯一约束保证事件幂等，以知识、内容版本和 ACL 版本阻止旧任务覆盖新状态。
10. 默认拒绝：隐私识别、脱敏或权限同步失败时，不发布可检索事件；受保护原文和附件内容不通过普通内容接口返回。

## 8. 相较初版的压缩说明

| 初版表或能力 | 第二版处理方式 |
|---|---|
| `iam.roles` | 一期角色固定，代码与 CHECK 约束管理，角色关系直接保存 `role_code` |
| `iam.auth_sessions` | 一期采用无状态 JWT；短期 Refresh Token、撤销和设备态放 Redis，不建立 PostgreSQL 登录会话表 |
| `iam.membership_exit_requests` | 当前退出申请字段并入 `organization_memberships` |
| `iam.access_request_reviews`、`iam.access_grants` | 单次审批和授权生命周期并入 `access_requests`；OpenFGA 保存在线关系 |
| `iam.outbox_events` | 一期 IAM 事件量较低，先由应用重试 / 对账；正式引入异步成员事件时应优先恢复 |
| `knowledge.knowledge_spaces` | 空间归属并入 `knowledge_bases` 和 `knowledge_items` |
| `knowledge.conversations` | 删除未接入会话总表；外部平台标识和会话元数据并入 `conversation_ingestions`，只在确认接入后落库 |
| `knowledge.collection_runs` | 运行状态和错误并入采集者；详细执行日志进入可观测系统 |
| `knowledge.collection_checkpoints` | 每个采集者对接入的游标并入 `conversation_collectors` |
| `knowledge.message_observations` | 原始观测精简后并入 `message_sources` |
| `knowledge.upload_batches` | 上传批次表删除；一期有意以 `attachments.request_id`、`upload_status` 和 `upload_error` 承载单文件上传幂等与状态 |
| `knowledge.knowledge_item_versions` | 一期内容固定版本 1，版本和就绪字段并入 `knowledge_items` |
| `knowledge.privacy_scan_tasks`、`knowledge.acl_sync_tasks` | 任务当前状态并入 `knowledge_items`，可靠触发由 `knowledge.outbox_events` 保证 |
| `rag.consumed_events` | 事件幂等并入 `processing_jobs.source_event_id` |
| `rag.parsed_artifacts` | 当前解析产物和工具版本并入 `processing_jobs` |
| `rag.qa_citations` | 一期引用数组并入 `qa_messages.citations JSONB` |

压缩的代价是部分表只保存“当前状态”，不适合直接承担复杂历史分析。审批、下载等安全历史由不可变审计日志保留；采集运行明细、任务阶段耗时和异常堆栈交给日志 / Trace / Metrics 系统。

## 9. 各 Schema 表清单

### 9.1 `iam`（8 张）

| 序号 | 表名 | 用途摘要 |
|---:|---|---|
| 1 | `iam.users` | 用户主体 |
| 2 | `iam.user_credentials` | 登录凭证 |
| 3 | `iam.organizations` | 组织主体 |
| 4 | `iam.organization_memberships` | 组织成员及退出审批生命周期 |
| 5 | `iam.membership_roles` | 成员固定多角色关系 |
| 6 | `iam.organization_invitations` | 一次性组织邀请 |
| 7 | `iam.access_requests` | 敏感原文 / 受保护附件申请、审批和授权 |
| 8 | `iam.audit_logs` | IAM 和权限审计 |

### 9.2 `knowledge`（11 张）

| 序号 | 表名 | 用途摘要 |
|---:|---|---|
| 1 | `knowledge.knowledge_bases` | 私人 / 组织知识库 |
| 2 | `knowledge.connector_accounts` | 个人外部平台绑定 |
| 3 | `knowledge.external_identities` | 外部身份及内部用户映射 |
| 4 | `knowledge.conversation_ingestions` | 已确认接入的私人会话 / 群聊及其平台标识 |
| 5 | `knowledge.conversation_memberships` | 已接入会话动态成员有效期 |
| 6 | `knowledge.conversation_collectors` | 主 / 补充采集者及检查点 |
| 7 | `knowledge.messages` | 统一规范化消息 |
| 8 | `knowledge.message_sources` | 多采集者观测来源 |
| 9 | `knowledge.attachments` | 附件元数据、对象及保护状态 |
| 10 | `knowledge.knowledge_items` | 知识资源、脱敏、版本和就绪状态 |
| 11 | `knowledge.outbox_events` | 知识服务事务事件 |

### 9.3 `rag`（5 张）

| 序号 | 表名 | 用途摘要 |
|---:|---|---|
| 1 | `rag.processing_jobs` | 消费幂等、解析、切块、向量化和索引任务 |
| 2 | `rag.index_records` | Elasticsearch 索引映射和状态 |
| 3 | `rag.search_history` | 搜索历史 |
| 4 | `rag.qa_conversations` | QA 会话 |
| 5 | `rag.qa_messages` | 问答消息和引用 |

总计：24 张业务及基础支撑表。

## 10. 与当前 Contract 的映射说明

当前 Contract 已支持私人知识、组织知识、本地上传、按条共享和七类事件。数据库字段与 Contract 的主要映射如下：

| Contract 字段 | 数据库字段 |
|---|---|
| `content_ref` | `knowledge.knowledge_items.content_ref`，即普通 / 脱敏展示内容引用 |
| `source.source_type` | `knowledge.knowledge_items.source_type`，使用 `platform_conversation`、`private_conversation`、`local_upload`、`shared_private_item` |
| `source.conversation_id` | `knowledge.conversation_ingestions.id` 的字符串值；平台原始会话 ID 保存在 `external_conversation_id` |
| `sharing.share_request_id` | `knowledge_items.share_request_id`，表示本次共享 API 请求的幂等键 |
| `sharing.share_batch_id` | `knowledge_items.share_batch_id`，表示本次通过 `message_ids` 选择的一组文字、文件或图片消息；单条共享时也生成批次 ID |
| `attachments.source_private_attachment_id` | `knowledge.attachments.source_private_attachment_id` |
| `attachments.acl_version` | `knowledge.attachments.acl_version` |

`access-check.schema.json` 的 `resource_type` 仍使用 `message`、`attachment`、`knowledge_item`、`spreadsheet`。敏感原文和附件内容在数据库与 OpenFGA 中可以使用更细的内部对象类型，但对外 Access Check 仍需由模块一统一映射，不能由模块二或模块三自行扩展动作。

Contract 的 `sharing` 对象用于描述一次被选中的消息共享；文字、文件和图片消息可以在同一批次中混合选择，系统只处理 `message_ids` 中的消息，不自动包含未选择的消息。一期不支持整个私聊持续共享，也不自动共享后续消息。
