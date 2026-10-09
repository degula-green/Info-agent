# 树形RAG 集成测试环境发现 交付文档

- **阶段**: 横向（验收环境的可信度）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 现象已定位并记录，测试失败信息已改成可诊断；隔离环境待建
- **关联文档**: 树形RAG-指标暴露与口径修正-交付文档.md、树形RAG-Phase0-地基改造-交付文档.md

## 1. 起因

测试套件里长期有 4 个 skipped，全是 `external integration disabled`——需要真实
PostgreSQL 与 Elasticsearch 的用例。中间件从本机可达，于是启用它们跑了一次。

## 2. 现象：端到端用例领不到 job

```text
MVPEndToEndIntegrationTests::test_postgres_and_elasticsearch_round_trip
  -> runtime.handle(event) 之后 claim_jobs("parse", job_id=...) 返回空
  -> 旧代码直接 [0] 取值，报 IndexError
```

起初怀疑是代码回归（这些用例从没在本地跑过），于是逐层排查。

## 3. 排查：有一个外部消费者在抢 job

**第一步，看失败瞬间那一行的状态**：

```text
status=retry_wait  current_stage=fetch
next_retry_at = 03:16:25.607   （未来 +4.8s）
lease_until   = 03:16:20.607   （已过期）
CURRENT_TIMESTAMP = 03:16:20.758
```

同一行四个谓词里 `next_retry_at <= CURRENT_TIMESTAMP` 为 **false**，所以不是
`claim_jobs` 的 bug——它老老实实拒绝了这条被别的进程置为"退避中"的记录。

**第二步，连续观测新建 job 的状态**：

```text
t+0s   一个已 retry_wait（retry_in=4.6）  两个 pending
t+1s   三个全部 retry_wait                 retry_in 递减
t+5s   retry_in 从 4.0 重新开始计数  ← 到期又被领取、又失败、又退避
```

这说明有个消费者在**持续领取并重试**这些 job。

**第三步，排除本地进程**：逐进程核对命令行，本机没有任何 rag worker（只有 agent 的
worker 与本仓库无关）；`app/main.py` 的 lifespan 只建容器、不跑 worker 循环。本机
没有消费者。

**第四步，看谁连着这个库**：

```text
39.97.235.59    13 sessions
61.54.104.60    13 sessions + DataGrip 2025.2.6 ×10 + 1 active
218.29.191.106   9 sessions + 1 idle-in-transaction
```

结论：**这是一个共享开发库**，另一个部署的 rag worker 正在消费
`rag_mvp.processing_jobs`。这同时解释了此前发现的那批"当前代码里根本不存在"的
`scope_export` 检索记录——它们来自在同一张表上工作的另一个构建。

## 4. 影响

| 影响 | 说明 |
|---|---|
| 集成用例必然随机失败 | 它假定自己独占 job 的生命周期。实测：20 次新建-领取里 3 次领不到；另一轮 10 次里 6 次连试三次都领不到。同一个用例时而通过时而失败，与代码无关 |
| 更糟的是会给别人灌噪声 | 这类用例会往共享表里插真实 job，对方的 worker 会真的领取、失败、重试。**测试动作会污染另一个部署的运行状态** |
| 结论方向被扭曲 | 失败看起来像"RAG 的 job 领取坏了"，实际是环境隔离问题；反过来，通过的用例也不能证明什么 |

排查期间我创建过的 job 已全部删除（每轮 `finally` 都按 `knowledge_item_id` 清理并打印
计数），但其中一部分确实被对方 worker 领取并重试过，这一点如实记录。

## 5. 处置

**没有改断言结论**——测试该失败还是失败，只是把裸 `IndexError` 换成可诊断的信息：

```text
job <id> 无法被本测试领取（lane=parse）。
最常见原因：这个库还有别的消费者在轮询 processing_jobs——
另一个部署的 rag worker 会在毫秒级抢走新 job 并置为 retry_wait，
于是本地领取为空。请改用隔离库/隔离 schema 后再跑该用例。
```

四处领取点统一走新的 `_claim()` 辅助方法。这样下一个人看到的是原因，而不是一个
下标越界。

## 6. 验收证据

```text
pytest -q（默认跳过外部集成）   ->  218 passed, 4 skipped
断言消息验证                     ->  用内存仓储直接触发，不触碰共享库
环境恢复                         ->  排查时停掉的本地 rag worker 已按原样重启
```

## 7. 未做与建议

| 项 | 说明 |
|---|---|
| 隔离环境 | 建议给集成测试独立的库或 schema（例如 `RAG_DATABASE_SCHEMA=rag_it` + 独立 ES 索引前缀）。在此之前，这四个用例只能在没有其它消费者时才可信 |
| CI 同理 | CI 里如果连的是同一个共享库，也会踩同样的坑 |
| 共享库的边界 | 现在本机开发、CI、另一个部署共用一个 PostgreSQL。这不是本仓库能单方面改掉的，但至少要知道：任何"独占扫描某张表"的测试在这里都不成立 |

## 8. 后续处理：远端 worker 已停

用户要求"把远端服务停了，避免抢 worker"，2026-10-09 已执行：

```text
远端主机      39.97.235.59（阿里云 ECS，hostname iZ2zeeceg32un8fblwra37Z）
compose 栈    docker-{core,knowledge,rag,agent}-service、agent-*-worker、nginx
停掉的服务    docker-rag-worker-1   ← 唯一消费 rag_mvp.processing_jobs 的进程
重启策略      unless-stopped        ← 手动停止后，宿主机重启也不会自己起来
保留运行      docker-rag-service-1（远端 API，不消费 job）
```

停止命令（在远端执行）：

```bash
docker stop docker-rag-worker-1
# 恢复：docker start docker-rag-worker-1
# 或 docker compose -f <compose 目录>/docker-compose.server.yml up -d rag-worker
```

**验证方式**（本地 worker 也临时停掉，否则会被本机自己领走）：

```text
1. 本地停 rag worker
2. 建一个 processing_jobs 行
3. 连续观察 4 秒

停止前：创建后约 0.2s 就被认领，状态在 retry_wait <-> processing 之间每 5s 循环
停止后：t+0 ~ t+3 全程 status=pending，lease_owner 为空 —— 没有任何消费者
```

验证用的 job 已删除（清理后行数 0），本地 rag worker 已按原样重启。

**跑那四个外部集成用例时，本地 worker 也必须停**——它们假定自己独占 job 生命周期，
本机的 worker 同样会抢：

```powershell
# 停本机 rag worker（只匹配 rag 的 worker.py，不影响 agent）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match '(^|[\s\\/])worker\.py' -and $_.CommandLine -notmatch 'agent' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## 8. 变更文件

```text
services/rag/tests/test_mvp_integration.py   四处领取点改为可诊断的 _claim 断言
services/rag/docs/树形RAG-集成测试环境发现-交付文档.md（新增）
```
