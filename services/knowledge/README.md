# Knowledge service

模块二的 Knowledge Go 服务，负责连接器账号、OAuth/微信配对、外部身份映射、会话接入、消息与附件元数据、采集状态和 Outbox 事件。

当前提供：

```text
GET /health
GET /api/info
GET/POST/DELETE /api/knowledge/v1/connectors...
GET/POST /api/knowledge/v1/conversations...
POST /api/knowledge/v1/internal/...
```

`/v1` 是保留的本地兼容别名；生产和前端联调使用 `/api/knowledge/v1`。

微信 Agent 只能通过内部签名接口上报，不能直接访问 PostgreSQL、Redis、MinIO 或 OpenFGA。微信附件内容以流式 multipart 上传，由 Knowledge 校验后写入对象存储。

## 本地启动

```powershell
go run ./cmd/server
```

服务默认监听 `http://localhost:8090`，可通过 `KNOWLEDGE_HTTP_PORT` 修改端口。

个人微信数据由 `agents/wechat-collector` 在用户电脑上读取并上报。本服务是业务规则和持久化的唯一入口，微信 Agent 不直接访问服务端数据库、Redis 或对象存储。
