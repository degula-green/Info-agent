# 服务三树形 RAG 最小闭环实施记录

## 本轮完成

- 服务二新增 RAG 状态字段和 `POST /internal/knowledge/{knowledge_item_id}/rag-result` 回调；支持认证、版本保护、终态保护和重复回调幂等，不修改原有采集接口。
- 服务三接入 `knowledge.ready` Redis Stream，复用 `rag-workers` 消费组；完成回源、Chunk、Fact 抽取/去重/版本、Session/Entity Tree、摘要、向量和 ES 投影。
- RAG Outbox 继续使用 `rag.outbox_events`，新增三类 Knowledge 回调事件并支持失败重试；不新增第二张 Outbox 表。
- 新增 `POST /api/v1/search/tree`；AI 问答自动选择 Chunk、Tree 或融合检索，支持多知识库过滤和来源 Chunk/Tree 路径引用。
- 新增 AI SSE、问答会话历史分页/详情/重命名/软删除；前端已切换真实 RAG API，保留 JSON 接口兼容。
- protected 索引和 ACL 字段保留，受保护正文继续 fail-closed，不写入 protected 真实正文；Core JWT/OpenFGA 本轮仍未接入 RAG 强校验。

## 验证结果

- RAG：31 项 `unittest` 全部通过，Python `compileall` 通过。
- Knowledge：新增 RAG 回调认证、参数校验、幂等、旧版本/终态保护和失败状态测试通过；全量测试仅有既有日期敏感 fixture 测试失败，与本轮改造无关。
- 前端：`npm run build` 通过。
- 远端 PG 已存在 memory 表、QA 扩展字段、处理阶段和 RAG Outbox 约束；远端 ES 四个 memory 读写别名可用，当前 Demo 为 8 个 Node、4 个 display Fact，protected 为 0。

## 待完成

- 用一条新的 Knowledge 文本消息做完整真实验收：采集、`knowledge.ready`、Redis 消费、回源、RAG 成功回调、Knowledge `rag_status=succeeded`、SSE 问答和历史落库。
- 生产模型、真实 Redis/Knowledge 运行配置及服务一/Core 授权联调。
- protected 附件正文回源、protected 实际投影和 OpenFGA 授权集合召回。
- 多轮 Agent 拆题、Scene Tree、人工事实审核、复杂实体合并和自动选择外部工具。
