# 模块二与模块三对接说明

## 1. 主流程

```text
模块二规范化消息、KnowledgeItem、附件元数据和对象引用
→ 保存归属、权限和安全状态
→ 同事务写入 Outbox
→ 发布 knowledge.ready 到 Redis Stream
→ 模块三消费并按资源 ID/版本通过 HTTP 回源
→ 文本处理或附件解析、切块、Embedding、写入 Elasticsearch
→ 发布 processing.completed 或 processing.failed
```

`knowledge.ready` 是一期建立索引的主触发事件。`document.processing.requested` 仅用于重解析、补偿或只解析任务，同一资源版本不得重复处理。

## 2. 服务边界

模块二负责规范化、业务数据保存、归属、安全状态和 OpenFGA 权限登记；不负责 MinerU、切块、Embedding 或 Elasticsearch。

模块三负责事件消费、HTTP 回源、文本/附件解析、切块、向量化、索引和处理结果通知；不直接读取模块二数据库，不修改业务归属或权限。

## 3. 必需对接信息

| 类别 | 最小信息 |
|---|---|
| `knowledge.ready` Payload | `processing_job_id`、`knowledge_item_id`、`content_version`、`acl_version`、`attachment_ids` |
| 附件信息 | `attachment_id`、`content_version`、`acl_version`、`content_access_required`、`object_ref`、`mime_type` |
| 事件信封 | `event_id`、`event_type`、`schema_version`、`occurred_at`、`trace_id`、`organization_id`、`producer`、`payload` |
| HTTP 回源 | `GET /internal/knowledge/{id}`、`GET /internal/knowledge/{id}/content`、`GET /internal/attachments/{id}`；响应结构、版本参数、服务认证和错误码 |
| 处理结果 | `processing_job_id`、资源 ID、内容/权限版本、`chunk_count`、`mapping_version` |
| 失败信息 | `stage`、`retryable`、`error_code`、`error_message`；不得携带正文或文件二进制 |
| Stream 约定 | Stream 名称、Consumer Group、ACK 时机、重试次数、死信 Stream、幂等规则 |

模块三建立索引时至少保存：

```text
organization_id
knowledge_item_id
content_version
acl_version
```

## 4. 一致性与安全

- 模块三按 `event_id` 幂等，并按 `knowledge_item_id + content_version` 防止旧任务覆盖新索引。
- HTTP 回源后校验资源和附件版本；版本不一致时停止处理并重新获取。
- 受保护内容只能进入受保护索引，查询前必须完成 OpenFGA 授权过滤。
- 事件不传大正文、文件二进制、密码、Token 或长期 MinIO 地址。
- 完成任务、写入结果事件并持久化处理状态后再 ACK；失败进入重试或死信流程。

## 5. 待定契约

以下内容需在 `docs/contracts/event-payloads/` 中分别定义：

```text
knowledge.ready
document.processing.requested
processing.completed
processing.failed
```
