# RAG 服务 MVP 实施记录

> 状态：Phase 0 至 Phase 7 主链路已实现
>
> 分支：`codex/rag-mvp-refactor`

## 1. 已落地内容

- 生产依赖只使用 `rag_mvp` Schema 和 `rag_chunks_*` 索引别名。
- 旧 `rag.memory_*` 代码和旧 ES mapping 已从 RAG 服务生产代码中删除。
- 旧 PG 表、旧 ES 物理索引和历史数据未删除、未迁移、未双写。
- `knowledge.ready` 使用冻结后的单资源 payload。
- 一个 KnowledgeItem 同时对应一个可处理资源和一条普通任务。
- 单 RAG Worker 实现 Dispatcher、Parse、Index、Memory、Callback Lane。
- PostgreSQL 任务状态和租约支持重复投递、进程恢复和阶段级重试。
- Parse Lane 先持久化 Chunk，Index Lane 再完成 Embedding 和 ES 写入。
- `ready` 只要求基础 Chunk、Embedding 和 ES 就绪，不等待 Memory。
- `metadata_only` 不生成 Chunk、Embedding、Branch Key 或 ES 文档。
- Chunk 使用确定性 ID，display/protected 使用逻辑位置去重。
- 检索使用 BM25、kNN、RRF、可选 Rerank 和最终权限复核。
- Tree Mode 支持 `off`、`shadow`、`boost`，默认 `shadow`。
- Branch Key 使用受控 Entity Registry 和确定性别名匹配，不调用 LLM。
- 候选 Entity 不进入正式树，支持审核、晋升、合并、忽略和暂缓。
- Branch Registry 变化后创建增量刷新任务。

## 2. 跨服务最小改动

- Knowledge 的 `knowledge.ready` 增加内部会话和来源受众字段。
- Knowledge 回源接口支持 `purpose=index`，允许 RAG 服务身份读取索引所需内容。
- Knowledge RAG 状态改为 `processing/ready/metadata_only/failed`。
- Core 授权接口增加组织、会话和 protected 精确对象范围。
- Core 复用组织管理员/信息管理员能力完成 Entity 审核授权。

## 3. 验证

本地命令：

```powershell
cd services/rag
.\.venv\Scripts\python.exe -m pytest -q

$env:RAG_MVP_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest tests/test_mvp_integration.py -q

$env:RAG_CROSS_SERVICE_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest tests/test_cross_service_integration.py -q
```

跨服务测试覆盖：

```text
Knowledge fixture
-> knowledge.ready
-> Redis Streams
-> RAG Dispatcher/Parse/Index
-> rag_mvp + rag_chunks_display_v1
-> Knowledge rag_status=ready
-> RAG public search
```

同时执行：

```powershell
cd services/knowledge
go test ./...

cd ../core
go test ./...
```

## 4. 后续非 MVP 项

Phase 8 仍后置，包括 Fact、Entity 自动归并、Node 摘要向量、语义树导航、KV
查询和 Agent 工具选择。
