# Agent 任务级并发与 Plan 内并行实施计划

## 1. 文档定位

本文档是可直接执行的实施计划，已经锁定关键实现选择。实现者不需要再决定调度模型、并发状态隔离、Outbox 领取方式或 Worker 启动方式。

实施分为两个可独立启用的阶段：

1. **任务级并发**：由 Worker Supervisor 启动 4 个独立 Task Worker 子进程。每个 Worker 同时只执行 1 个 Task，不同 Task 可以并行，同一 Task 仍由租约保证只被一个 Worker 执行。
2. **Plan 内并行**：将 PlanStep 扩展为显式无环 DAG。同一个 Task 最多并发执行 4 个无依赖只读步骤；写步骤独占执行。

首期只处理本地开发环境，以 `start-dev.ps1` 为启动入口。Docker 和跨机器 Worker 伸缩不在本轮范围。

## 2. 已锁定决策

- 并发状态隔离：Provider 和 LLM Client 使用 ContextVar 与调用返回值隔离状态，不使用全局锁，也不为每个 Step 创建独立 Provider。
- Task 状态提交：Step 线程只执行并返回结果，由 Task 调度器统一提交 Task、Checkpoint、事件和预算；累计字段使用原子 SQL 更新。
- Outbox 领取：复用现有 `status` 和 `available_at`，增加 `publishing` 状态和过期重领，不新增租约字段。
- Worker 启动：新增 Worker Supervisor，在一个进程/窗口中启动 4 个独立 Worker 子进程，统一管理 Consumer Name、日志和退出信号。
- 上线策略：任务级并发和 Plan 内并行分别配置，Plan 内并行默认关闭，功能测试和压测通过后再显式启用。
- 验收目标：支持 10 个任务在 1 秒内到达；任务排队等待 P95 不高于 5 秒；包含 3 个独立网页检索的任务端到端耗时下降至少 50%；数据库连接和 LLM/RAG 错误率不高于启用前。

## 3. 配置项

新增配置：

- `AGENT_TASK_WORKER_PROCESSES=4`
- `AGENT_TASK_STEP_CONCURRENCY=4`
- `AGENT_TASK_PARALLEL_ENABLED=false`
- `AGENT_OUTBOX_CLAIM_LEASE_SECONDS=60`

兼容规则：

- `AGENT_TASK_WORKER_PROCESSES` 优先。
- 未设置时回退到现有 `AGENT_WORKER_CONCURRENCY`。
- `AGENT_WORKER_CONCURRENCY` 标记为废弃字段，不再由 Worker 内部读取，只作为启动脚本的回退值。

连接池规则：

- Agent API 使用现有 `AGENT_DATABASE_MAX_POOL_SIZE`，默认不超过 10。
- Task Worker 子进程由 Supervisor 覆盖为最多 6 个连接。
- Knowledge Worker 保持低并发配置，最多 4 个连接。
- 同一个 Agent 进程内 AgentStore 和 TodoStore 必须共用同一个 ConnectionPool。

## 4. 任务级并发

### 4.1 Worker Supervisor

- 新增 `worker_supervisor.py`，作为本地 Task Worker 的唯一入口。
- Supervisor 读取 `AGENT_TASK_WORKER_PROCESSES`，启动对应数量的 `worker.py` 子进程。
- 每个子进程接收唯一参数 `--consumer-name task-worker-<index>`。
- 每个子进程使用独立 AgentContainer、独立 Runtime 和独立 ConnectionPool。
- Supervisor 负责统一转发退出信号、收集子进程退出码、记录子进程日志前缀并在任一子进程异常退出时报告。
- `start-dev.ps1` 不再直接启动 `worker.py`，改为启动 `worker_supervisor.py`。
- `knowledge_worker.py` 仍保持单实例。
- `Stop-AgentServiceChain` 需要同时识别 `worker_supervisor.py`、`worker.py` 和 `knowledge_worker.py`，并清理 Supervisor 及其子进程。
- 启动后的健康检查必须确认 Supervisor 存在，并且 Task Worker 子进程数量等于配置值。

### 4.2 Redis Consumer

- Task Worker 的 Consumer Name 使用 CLI 参数；没有参数时才回退为 `hostname-pid`。
- 多个 Worker 共享同一个 Redis Consumer Group。
- 继续使用现有 Task Lease 保证同一个 Task 只能被一个 Worker 执行。
- `xautoclaim` 保持启用，用于回收崩溃 Worker 留下的 pending 消息。

### 4.3 Outbox 原子领取

Outbox 状态流转固定为：

```text
pending -> publishing -> sent
                    \-> pending（发布失败或租约过期）
```

领取规则：

- 使用 `FOR UPDATE SKIP LOCKED` 原子领取一批 `pending` 或租约过期的记录。
- 领取成功后写入 `status=publishing`。
- `available_at` 同时作为本次发布租约的到期时间，值为当前时间加 `AGENT_OUTBOX_CLAIM_LEASE_SECONDS`。
- 发布成功后标记 `sent` 并写入 `published_at`。
- 发布失败时恢复为 `pending`，增加 `attempt_count`，按指数退避更新 `available_at`。
- Worker 在发布完成后、标记 sent 前崩溃时允许重新发布。系统继续采用至少一次投递语义；Task 唤醒依赖租约实现幂等。
- 原有只覆盖 `status='pending'` 的索引需要替换为同时覆盖 `pending` 和 `publishing` 的 `(status, available_at)` 部分索引。

## 5. Plan DAG 契约

### 5.1 数据语义

`PlanStep` 新增：

- `depends_on: list[str] | None`

解释规则：

- `None`：旧计划或未声明依赖，默认依赖 `order` 小于当前步骤且最大的前一步骤。
- `[]`：明确没有依赖，可以立即进入 ready。
- 非空列表：只有列表中的全部 Step 都成功后，当前 Step 才进入 ready。

`order` 继续表示稳定展示顺序，不单独代表执行依赖。

### 5.2 Planner 输出

LLM Planner 草稿新增：

- `depends_on: list[int] | None`

规则：

- 数字引用当前 Plan 中更早步骤的序号。
- Runtime 绑定阶段把序号转换为真实 `step_id`。
- 未输出该字段时保持 `None`，兼容现有串行计划。
- 确定性 Planner 首期继续输出 `None`，不主动生成并行 DAG。

LLM Planner 可以生成任意无环 DAG，但必须遵守 Runtime 统一校验，不允许绕过写屏障或并发上限。

### 5.3 DAG 校验

Plan 保存前必须验证：

- 所有依赖都存在。
- 不允许自依赖。
- 不允许依赖未来步骤。
- 不允许重复依赖。
- 不允许环。
- `$steps.<step_id>` 引用对应的步骤必须自动加入依赖集合。
- 如果显式依赖与引用产生的依赖冲突，Plan 无效。
- 总 Step 数量不超过现有限制。

### 5.4 并行资格

- 所有 `side_effect=false` 的 Capability 都允许并发。
- 所有 `side_effect=true` 的 Capability 都禁止并发。
- 一个写步骤 ready 时，它是独占屏障：必须等到当前运行的兄弟步骤全部结束，且执行期间不能启动其他新步骤。
- 多个写步骤按照稳定 `order` 逐个执行。

所有只读 Capability 放开并发后，Provider、LLM Client 和 Capability 必须满足线程安全要求，不允许依赖实例级可变调用状态。

## 6. Runtime 调度算法

启用 `AGENT_TASK_PARALLEL_ENABLED=false` 时，继续使用当前串行逻辑，保证完全向后兼容。

启用后的单轮调度流程固定为：

1. 读取 Task、Plan 和全部 Step，完成依赖标准化。
2. 如果存在 running Step，先执行崩溃恢复，不启动新 Step。
3. 计算 ready set：状态为 `pending` 或 `ready`，并且全部依赖均为 `succeeded`。
4. 如果 ready set 中有写步骤，等待所有 running Step 结束，然后只启动 order 最小的写步骤，本轮不启动其他 Step。
5. 如果没有写步骤，从 ready set 中按 order 选择最多 `AGENT_TASK_STEP_CONCURRENCY` 个只读步骤。
6. 使用 Task 自己的 `ThreadPoolExecutor` 并行执行选中步骤。
7. 每个 Step 线程只执行 Capability、处理该 Step 的 transient retry，并返回执行结果；不直接更新 Task、Checkpoint、Budget 或事件。
8. 调度器等待本轮所有已启动 Step 结束，并收集成功、失败和 unknown 结果。
9. 调度器统一写入 Observation、Step 状态、Checkpoint、Task 状态和事件。
10. 如果任一 Step 失败，不再启动新的兄弟步骤；等待本轮所有已启动步骤结束后，把完整 Observation 集合交给 Planner。
11. 如果本轮全部成功，回到第 1 步继续计算 ready set。
12. 如果没有 ready 且没有 running，进入现有 Planner decision 流程。

线程数量规则：

- 每个 Task 使用独立的有界 `ThreadPoolExecutor`。
- `max_workers` 固定为 `AGENT_TASK_STEP_CONCURRENCY`。
- Task 执行结束后必须关闭线程池。
- 不允许不同 Task 共用可变执行线程池。

## 7. 状态、恢复与并发安全

### 7.1 Task 状态提交

- Task 的 `status`、`checkpoint`、`step_count`、`model_call_count` 和 `updated_at` 由调度器统一更新。
- `step_count` 和 `model_call_count` 使用原子 SQL 自增。
- Step 状态更新只允许作用于自己的 `step_id`。
- 事件写入继续使用数据库自增 sequence，不依赖内存顺序。
- Observation 保存后，调度器才能把成功 Step 标记为 succeeded。

### 7.2 ContextVar 调用状态

- Provider 和 LLM Client 不再通过 `self.last_call_count` 跨调用传递计数。
- 每次模型调用在 ContextVar 中维护调用计数，并在调用结束时恢复 token。
- Capability 返回的 `model_calls` 必须来自调用局部结果，不读取共享实例状态。
- Planner 和 Understanding Provider 的调用计数也使用相同模式，避免后续并发扩展留下隐患。

### 7.3 Checkpoint 与恢复

`Checkpoint` 扩展为：

- `completed_step_ids`
- `ready_step_ids`
- `running_step_ids`
- `next_step_id`

`next_step_id` 只保留兼容和展示用途。

恢复流程：

1. 读取 Plan 全部 Step。
2. 对每个 `running` Step 查询 CapabilityCall。
3. 已完成调用直接恢复为 succeeded/failed/unknown Observation。
4. 没有 CapabilityCall 的 running Step 恢复为 ready。
5. 根据依赖重新计算 ready set。
6. 继续执行调度循环。

### 7.4 审批与输入

- 写步骤进入审批前必须保证没有 running 兄弟步骤。
- waiting_approval 或 waiting_input 时不允许启动新的兄弟步骤。
- 审批通过后，恢复该写步骤，并重新计算 ready set。
- 并行只读步骤不会触发审批。

## 8. 持久化与迁移

### 8.1 Plan Step

为 `agent.agent_plan_steps` 增加：

- `depends_on JSONB NULL`

旧数据保持 `NULL`，按顺序依赖解释。新 DAG 计划中显式无依赖保存为 `[]`。

### 8.2 Outbox

不新增租约字段。

- 复用 `status` 和 `available_at`。
- 替换现有部分索引，使 query 能同时覆盖 `pending` 和 `publishing`。

### 8.3 Store 接口

增加以下能力：

- 原子领取 Outbox 批次。
- 发布失败后恢复 pending 并退避。
- 原子增加 Task 的 step_count 和 model_call_count。
- 一次性读取某 Task 的全部 ready/running Step。

## 9. 文件级实施范围

### 9.1 Kernel

- `app/kernel/models.py`：增加 `depends_on`、Checkpoint 新字段和 Step 执行结果模型。
- `app/kernel/validator.py`：增加 DAG 校验。
- `app/kernel/bindings.py`：引用自动补充依赖。
- `app/kernel/checkpoint.py`：支持多 ready/running Step。
- `app/kernel/runtime.py`：实现 DAG 调度、并发批次、写屏障、统一提交和恢复。
- `app/kernel/executor.py`：拆分为线程内执行和调度器提交两个阶段。

### 9.2 Planning

- `app/planning/llm.py`：Planner Draft 增加数值型依赖。
- `app/planning/schema.py`：严格 JSON Schema 增加 `depends_on`。
- `app/planning/deterministic.py`：保持 `None`，不改变现有行为。
- `app/planning/routing.py` 和知识路由：保持现有 Plan 生成逻辑，只有接到显式 DAG 时并行。

### 9.3 Worker 与 Redis

- `services/agent/worker.py`：增加 Consumer Name 参数，支持作为子进程运行。
- `services/agent/worker_supervisor.py`：新增 Supervisor。
- `app/infrastructure/redis/streams.py`：适配唯一 Consumer 和 Supervisor 子进程。
- `scripts/start-dev.ps1`：改为启动 Supervisor，更新停止、日志和健康检查。

### 9.4 Store 与 Outbox

- `app/infrastructure/postgres/store.py`：增加原子 Outbox 领取、原子计数和 Checkpoint 查询。
- PostgreSQL migration：增加 `depends_on` 和新的 Outbox 索引。

### 9.5 Provider 并发安全

- `app/infrastructure/llm/client.py`：调用计数改为 ContextVar/返回值。
- `app/infrastructure/laya/client.py`：调用计数改为调用局部状态。
- `app/providers/answer.py`、`app/providers/chat.py`：移除跨调用共享计数。
- `app/understanding/provider.py` 和 `app/understanding/hybrid.py`：使用相同隔离策略。
- `app/capabilities/knowledge.py`：保留已有 scope 并行逻辑并确认线程安全。
- `app/capabilities/web_research.py`：确认多实例并发请求安全。

### 9.6 配置与文档

- `app/config.py`：增加新配置，保留旧配置回退。
- `services/agent/.env.example`：加入新配置和说明。
- `scripts/README.md`：更新 Agent 进程数量和启动说明。

## 10. 实施顺序

1. **配置与观测**：新配置、指标、旧配置回退；不改变运行行为。
2. **任务级并发**：Supervisor、多 Worker、唯一 Consumer、共享连接池。
3. **Outbox 原子领取**：状态流转、租约复用、索引迁移和并发测试。
4. **DAG 模型与持久化**：`depends_on`、Checkpoint、迁移和校验。
5. **Runtime 调度**：ready set、写屏障、线程池、统一提交和恢复。
6. **并发安全改造**：ContextVar、原子计数和 Provider 调用状态。
7. **功能测试与压测**：先关闭 DAG 开关回归，再开启 4+4 压测。
8. **本地默认启用**：确认验收指标后开启 `AGENT_TASK_PARALLEL_ENABLED=true`。

## 11. 测试计划

### 11.1 单元测试

- `depends_on=None` 保持顺序依赖。
- `depends_on=[]` 可以并行。
- 多依赖、扇入、扇出、循环、自依赖、未来依赖和缺失依赖。
- `$steps` 引用自动转为依赖。
- 写步骤独占和多个写步骤稳定排序。
- ContextVar 并发模型调用计数互不干扰。
- 原子计数并发增加不丢数据。

### 11.2 集成测试

- 4 个 Worker 同时处理 4 个 Task。
- 同一 Task 重复唤醒只执行一次。
- 多个 Worker 竞争 Outbox 不产生额外重复发布。
- 发布中 Worker 崩溃后，租约到期可重领。
- 并行只读 Step 的 SSE 事件完整且 sequence 唯一。
- 并行组中一个 Step 失败，其他 Step 完成后再产生一次 Planner decision。
- 多个 running Step 的崩溃恢复。
- 写步骤等待审批时没有兄弟步骤启动。
- 功能开关关闭时全部旧测试行为不变。

### 11.3 压测场景

- 1 秒内提交 10 个独立任务。
- 每个任务包含 2-4 个无依赖只读步骤。
- 任务中至少包含一次 LLM 调用和一次 RAG 或网页调用。
- 持续运行 10 分钟并记录峰值。

## 12. 验收标准

必须全部满足：

- 10 个任务同时到达时，不出现任务永久排队或租约冲突。
- 队列等待时间 P95 不高于 5 秒。
- 包含 3 个独立网页检索的任务，端到端耗时比串行版本下降至少 50%。
- 写步骤没有出现并行执行。
- 没有重复创建待办或重复写入表单。
- 并行恢复不重复执行已成功的 CapabilityCall。
- `model_call_count` 和 `step_count` 与 Observation 数量一致。
- PostgreSQL 没有连接超时；连接池峰值利用率低于 80%。
- LLM、RAG、网页服务的错误率不高于启用前。
- `AGENT_TASK_PARALLEL_ENABLED=false` 时，`services/agent/tests` 全部通过。

## 13. 回滚策略

- 任务级并发通过 `AGENT_TASK_WORKER_PROCESSES` 回退到 1。
- Plan 内并行通过 `AGENT_TASK_PARALLEL_ENABLED=false` 立即回退。
- Outbox 状态兼容旧的 `pending/sent` 消费方式；回滚时 `publishing` 记录由租约到期恢复到 pending。
- DAG 字段保持可空，旧代码可忽略；不执行数据回滚。
- 回滚不删除 `depends_on`、Checkpoint 新字段或 Outbox 新索引。
