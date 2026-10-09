# 树形RAG 配置面治理 交付文档

- **阶段**: 横向治理（不属于某个 Phase 的验收项，但直接决定配置是否可信）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 残留配置已删、失效的 compose 注入已清理、防回潮测试已就位；31 个保留项待 owner 复核
- **关联文档**: 树形RAG-指标暴露与口径修正-交付文档.md、树形RAG-灰度白名单-交付文档.md、树形RAG-监控抓取与告警规则-交付文档.md

## 1. 为什么做这件事

前面几轮陆续撞到同一类缺陷：**配置声明了、.env 里也写了，但代码从来不读**。

```text
RAG_METRICS_ENABLED              声明了但无人读 -> /metrics 端点根本不存在
RAG_TREE_SHADOW_SAMPLE_RATE      声明了但无人读 -> 采样率是个装饰
ELASTICSEARCH_DISPLAY_INDEX      compose 注入但应用不读 -> 改索引名静默失效
```

每次都是"看起来配了、实际没生效"，而且失败是静默的。所以这次把它做成一次系统排查，
而不是等下一次撞上。

方法：解析 `config.py` 里全部 **204** 个设置声明，逐个在服务代码里查找
`settings.<name>` 的实际引用。

## 2. 结果：34 个没有读取方

其中 3 个属于"被新名字取代、旧的没删"，直接删除：

| 删除项 | 为什么 |
|---|---|
| `internal_auth_token`（`RAG_INTERNAL_AUTH_TOKEN`） | 与 `rag_internal_token`（`RAG_INTERNAL_TOKEN`）并存。两个 token 设置摆在一起，运维设错那个完全没有任何反馈 |
| `elasticsearch_display_index` / `elasticsearch_protected_index` | 被 `..._READ_INDEX` / `..._WRITE_INDEX` 别名读写分离取代 |

同时清掉 `docker/docker-compose.yml` 里两处（rag 与 rag-worker）注入的
`ELASTICSEARCH_DISPLAY_INDEX` / `ELASTICSEARCH_PROTECTED_INDEX` 共 4 行：这两个变量
是应用**不再读**的旧名字，留着会让"照 compose 改索引名"变成一个静默无效的操作。
新版 `..._READ_INDEX` / `..._WRITE_INDEX` 本来就在同一段环境变量里，去掉旧行不影响默认值。

## 3. 剩下 31 个：登记而不是假装

没有直接删完。原因是"哪些旋钮属于预留能力、哪些是纯遗留"是 owner 的判断，不是可以
替代码做主的决定。测试文件里按原因分了类：

```text
部署形态提供（应用不读）        http_host / http_port（uvicorn 参数由启动命令给）
超时按调用传参                  *_connect_timeout_seconds（6 个，客户端按次收 timeout）
重试写死在实现里                embedding_max_retries / extract_max_retries
规划中未实现                    rerank_*（6）、query_rewrite_*（2）、otel_*（2）
遗留旋钮待确认                  authz_*、redis_dlq_stream、index_*、worker_concurrency、
                                task_visibility_timeout_seconds、search_deadline_ms、
                                final_top_k、highlight_final_only 等
```

登记的意义是：这些"设置了也不生效"的旋钮现在是**显式的已知状态**，不再是看起来正常的
隐患。

## 4. 防回潮：三条断言

`tests/test_config_surface.py`

```text
1. 声明但无读取方的设置集合 必须等于 登记表
   -> 新加了没人读的设置会失败；给登记项接上代码后忘记移除也会失败
2. 扫描器自检：解析出的设置数量必须 > 150，且包含 tree_mode / rag_internal_token
3. 本套测试的起因（metrics_enabled / tree_shadow_sample_rate /
   memory_regex_candidates_enabled）必须真的有读取方，且不在登记表里
```

第 2 条是这类元测试最容易自欺的地方：如果 `config.py` 换了写法导致正则匹配不到任何
设置，前两条断言会**同时通过**（空集等于空集），测试看起来一切正常却什么都没检查。

## 5. 验收证据

```text
pytest -q                  ->  218 passed, 4 skipped
解析设置数                  ->  204
无读取方                    ->  34（删除 3 个后剩 31，全部登记）
服务重启后                  ->  /health 200、/metrics 200（27 个指标族，Bearer 鉴权正常）
```

删除配置属于"运行时行为不变"的改动（本来就没被读），但仍实测重启确认配置类没有被
别的地方间接依赖。

## 6. 未做与后续

| 项 | 说明 |
|---|---|
| 31 个保留项的最终处置 | 建议逐个过一遍：能接的接（例如超时其实应该从配置传给客户端），该删的删，明确预留的留在登记表里。测试只保证"不会无声增长"，不判断该不该留 |
| `rerank_*` 与 .env | `services/rag/.env` 里有 `RERANK_*` 配置，但代码没有 rerank 实现。要么落实现，要么把 .env 里也清掉（.env 未入库，属环境侧） |
| 配置来源单一化 | 当前设置既可能来自 .env、也可能来自 compose 注入，两者不一致时以进程环境为准。这次清掉了一处不一致，但没有机制能自动发现下一处 |

## 7. 变更文件

```text
services/rag/app/config.py                    删除 3 个被取代的残留设置
docker/docker-compose.yml                     删除 2 处共 4 行失效的 ES 索引注入
services/rag/tests/test_config_surface.py     配置面无读取方守卫（新增 3 例）
```
