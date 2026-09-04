# Knowledge service

模块二的 Go 服务骨架，负责未来的连接器编排、消息采集、会话与附件管理、知识对象生成及处理状态协调。

当前只提供两个占位接口，不连接数据库、Redis、MinIO、飞书或微信：

```text
GET /health
GET /api/info
```

## 本地启动

```powershell
go run ./cmd/server
```

服务默认监听 `http://localhost:8090`，可通过 `KNOWLEDGE_HTTP_PORT` 修改端口。

个人微信数据由 `agents/wechat-collector` 在用户电脑上读取并在后续版本中上报。本服务是业务规则和持久化的唯一入口，微信 Agent 不直接访问服务端数据库、Redis 或对象存储。
