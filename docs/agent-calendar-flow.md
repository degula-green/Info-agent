# Agent 日程创建流程

## 1. 范围

第一期能力：Knowledge 采集飞书消息后，Agent 判断消息是否包含日程意图，生成待确认候选；用户编辑并确认后，由 Knowledge 调用飞书日历 API 创建日程。

本阶段不包含主动 Agent 对话、网页总结、表格填写、参会人邀请和复杂多轮任务。

## 2. 服务边界

- Core：身份、组织和 JWT。
- Knowledge：飞书 OAuth、Token、消息采集、消息存储和飞书 API。
- Agent：Runtime、模型判断、任务状态、审批和工具编排。
- RAG：检索、文档处理和问答，不参与第一期日程创建。
- Web：展示候选、编辑、确认或拒绝。

Agent 不直接访问 Knowledge 数据库，不保存飞书 Token，也不直接调用飞书 API。

## 3. 总体交互

1. 浏览器向 Core 登录并获得 RS256 JWT。
2. Knowledge Worker 轮询飞书并保存消息。
3. Knowledge 发布 `knowledge.ready` 到 Redis Stream。
4. RAG 使用 `rag-workers` 消费；Agent 使用独立的 `agent-calendar-workers` 消费。
5. Agent 通过受保护的内部接口调用 Knowledge。
6. Knowledge 使用 Token Vault 调用飞书日历 API。

## 4. 采集消息到候选日程

### 4.1 身份和授权

Agent API 验证 Core 的 RSA 公钥，使用 JWT 的 `sub` 识别当前用户。配置使用 `AGENT_JWT_PUBLIC_KEY`、`AGENT_JWT_ISSUER=info-agent-core` 和 `AGENT_JWT_AUDIENCE=info-agent-api`，不使用不存在的 `AGENT_JWT_SECRET`。

Knowledge 已负责飞书 OAuth、账号绑定、Token 加密保存、Token 刷新和消息轮询。Agent 不保存 Access Token 或 Refresh Token。

### 4.2 Knowledge 事件

Knowledge 只有在内容、归属、安全和权限状态准备完成后，才发布 `knowledge.ready`。事件包含 `knowledge_item_id`、`source_message_id`、`content_version` 和 `acl_version` 等资源引用，但不包含正文。

### 4.3 Agent Worker 过滤

Agent 首次创建 Consumer Group 时从 `$` 开始，避免首次部署重新处理历史消息。只处理以下事件：`knowledge.ready`、存在 `source_message_id`、来源是飞书、消息类型为文本、由当前用户本人发送，并且该消息版本尚未处理。

### 4.4 获取正文

Agent 调用 `GET /api/knowledge/v1/internal/agent/messages/{message_id}`，使用 `Authorization: Bearer <KNOWLEDGE_INTERNAL_SERVICE_TOKEN>` 和 `X-Caller-Service: agent`。

Knowledge 返回消息正文、会话、发送人、消息类型、时间、组织和 `is_owner_message`。`is_owner_message` 必须由 Knowledge 判断，Agent 不自行比较飞书外部 ID。

### 4.5 Runtime 判断

流程是：消息快照 → Context Manager → LLM Adapter → 结构化输出校验 → 候选或补充信息。

模型可以使用 OpenAI-compatible Chat Completions，但 Agent Adapter 必须支持结构化输出、Tool Calling 和参数校验。

例如“今天晚上八点开会”应提取为 `calendar.create`，标题为“开会”，开始时间为当天 20:00，默认结束时间为 21:00，时区为 `Asia/Shanghai`。

默认规则：没有“提醒我”也可以识别；结束时间缺失默认一小时；标题缺失使用消息摘要；开始时间缺失进入 `needs_input`，不能直接创建。

## 5. 用户确认和工具执行

候选保存到 Agent 数据库，状态为 `waiting_approval`，至少包含标题、开始时间、结束时间、时区、地点、描述、来源消息、版本号和过期时间。

Nginx 新增 `/api/agent/`。Web 单独维护 Agent Proposal，不混入现有 RAG Mock QA 数据。建议接口为：

- `GET /api/agent/v1/proposals`
- `GET /api/agent/v1/proposals/{id}`
- `PATCH /api/agent/v1/proposals/{id}`
- `POST /api/agent/v1/proposals/{id}/confirm`
- `POST /api/agent/v1/proposals/{id}/reject`
- `GET /api/agent/v1/runs/{id}`

所有接口按 JWT 的 `sub` 限制当前用户。编辑时递增 `proposal_version`；确认时提交版本号，防止旧页面或重复点击执行错误数据。

确认链路是：Agent API 更新审批状态 → 创建 `agent_tool_calls` → Tool Registry 选择 `calendar.create` → Agent Worker 执行工具。

## 6. 创建飞书日程

Agent 调用 `POST /api/knowledge/v1/internal/agent/feishu/calendar/events`，携带服务 Token 和 `X-Caller-Service: agent`。请求只包含 `user_id`、`title`、`start_at`、`end_at`、`timezone`、`location`、`description` 和 `request_id`。

Knowledge 根据用户查找连接器，从 Token Vault 获取或刷新 Token，调用飞书 Calendar API，并返回 `event_id` 和 `event_url`。飞书日历 Scope 和请求字段必须依据官方 API 文档确认，不能直接沿用当前消息采集 Scope。

实际平台调用链是：Agent → Knowledge 内部日历接口 → Knowledge Token Vault → 飞书 Calendar API。

## 7. Agent 数据库

不新建 PostgreSQL 实例，继续使用数据库 `info_agent`，新增 `agent` schema。

建议第一期表：

- `agent_runs`：保存一次执行、触发事件、来源消息、版本、用户、组织、状态和错误。对 `agent_type + source_message_id + content_version` 建立唯一约束。
- `agent_messages`：保存 Runtime 中的系统、用户、模型和工具消息；正文控制保留范围。
- `agent_tool_calls`：保存工具名、参数、结果、状态、尝试次数、外部日程 ID 和执行时间。
- `agent_approvals`：保存候选、审批状态、`proposal_version`、过期时间、确认时间和拒绝时间。

建议状态包括：`received`、`running`、`ignored`、`needs_input`、`waiting_approval`、`executing`、`succeeded`、`failed`、`unknown`、`cancelled`。

迁移应通过独立 `agent-migrate` 执行，不能手动直接修改生产数据库。

## 8. 异常处理

- 用户拒绝：Approval 为 `rejected`，Run 为 `cancelled`。
- 缺少开始时间：Run 为 `needs_input`。
- 模型失败、连接器不存在或权限不足：标记 `failed`。
- 飞书请求超时或结果不确定：标记 `unknown`，不能盲目重试，先提示用户去飞书确认。
- Token 过期：由 Knowledge 刷新后再处理一次。

## 9. 第一阶段部署和验收

需要新增 `services/agent`、`agent-api`、`agent-worker`、`agent-migrate` 和 `apps/web/src/api/agent.ts`；需要修改 `docker/docker-compose.yml` 与 `gateway/nginx/conf.d/default.conf`。第一阶段不修改 RAG 表、RAG Consumer Group 或 QA 数据模型。

最低验收标准：本人飞书文本消息可以产生一个 Run；重复事件不重复生成候选；他人消息和非文本事件被忽略；“今天晚上八点开会”能生成 20:00 候选；用户可编辑、确认或拒绝；旧版本不能确认；确认只创建一次；Token 刷新和授权错误可处理；未知外部结果不自动重复创建；用户只能看到自己的数据。
