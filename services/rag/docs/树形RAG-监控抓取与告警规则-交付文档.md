# 树形RAG 监控抓取与告警规则 交付文档

- **阶段**: Phase 2 补齐（Week 8 Day 3-4「指标与看板」——抓取与告警部分）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 抓取配置与告警规则完成并校验；**Grafana 面板与监控栈本身未部署**（见第 5 节）
- **关联文档**: 树形RAG实施计划.md (v2.5 §4.2.3)、树形RAG-指标暴露与口径修正-交付文档.md

## 1. 起点：端点有了，抓取合同还没有

`/metrics` 已经能出 27 个指标族，但"谁来抓、怎么鉴权、抓多快"这三件事没定，等于只有
一个能被 curl 的端点。引入 Prometheus/Grafana 属于新增部署组件、需要决策，但**抓取
合同和告警阈值不该等这个决策**——它们定义的是端点之外的接口约定。

## 2. 过程中发现的一个真问题：配置写出来会是死的

Prometheus 的 scrape config **没有自由 header 字段**，它能发出去的只有
`Authorization: Bearer`（或 basic auth）。而 `/metrics` 只认
`X-RAG-Internal-Token`。也就是说，照现状写出的抓取配置**永远 403**，而且失败原因会
显示成"鉴权失败"，不会指向"这个客户端根本发不出你要的头"。

修法：端点同时接受两种形状。

```text
X-RAG-Internal-Token: <token>      保留（内部调用、curl 调试）
Authorization: Bearer <token>      新增（Prometheus 只能发这个）
```

比较改用 `hmac.compare_digest`——这是一个共享密钥，用 `!=` 比较会在时间上泄露长度与
前缀。

## 3. 交付物

### 3.1 抓取配置（`monitoring/prometheus-scrape.example.yml`）

```yaml
scrape_configs:
  - job_name: rag
    scrape_interval: 60s
    scrape_timeout: 30s
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/rag-internal-token
    static_configs:
      - targets: ["rag:8000"]
```

两个数字不是随手写的：一次抓取要为每个活跃 scope 重跑一遍 snapshot（每个 scope 若干条
查询），实测 15 个 scope 约 9~10s。所以 `scrape_timeout` 必须明显高于它（10s 会在刚
跑完时被判超时），`scrape_interval` 必须高于 timeout（否则抓取互相重叠）。

Token 走 `credentials_file` 而不是内联，避免它出现在配置和配置导出里。

### 3.2 告警规则（`monitoring/rag-tree-alerts.yml`）

7 条规则，阈值与服务内已有的护栏（`evaluate_alerts`）保持一致，使同一状态既体现在
`rag_tree_alert_count` 序列上，也有 Prometheus 侧的触发历史：

| 告警 | 表达式要点 | 为什么 |
|---|---|---|
| `RagTreeGuardRailFiring` | `rag_tree_alert_count > 0` | 服务内护栏的汇总入口，一条覆盖全部 |
| `RagTreeCandidateBacklog` | `pending_candidate_count > 100` | 审核跟不上扫描，树无法增长 |
| `RagTreeNoMounts` | 有消息、有实体、`mount_count == 0` | 注册表有实体却没有任何 chunk 能到达 |
| `RagTreeEntitiesMissingEmbedding` | 缺向量占比 > 20% | L3 对无向量实体无能为力 |
| `RagTreeHighNoEntityMatch` | 检索数 > 20 且命中率 < 50% | 树只增加往返没收窄范围 |
| `RagTreeHighEmptyWindowRate` | 窗口数 > 10 且空窗率 > 50% | 模型在花钱却不产挂载 |
| `RagTreeMetricsUnreachable` | `up{job="rag"} == 0` | 抓不到指标本身也是故障 |

两条比率型告警都带最小样本量，避免冷启动时被 0 值误报。

## 4. 验收证据

```text
pytest -q   ->  215 passed, 4 skipped
```

**规则与序列的交叉校验**（`tests/test_monitoring_config.py`）：解析告警文件，抽出所有
`rag_tree_*` 引用，与 `flatten_metrics({})` 实际产出的序列名求差集，多余或写错的序列
直接失败。这条测试的价值在于它会在**改动指标名时**立刻抓住忘记改规则的情况——否则规则的
表达式会静默变成永远不触发的空查询。

真实端点四种鉴权路径实测（RAG 本地 8000，27 个指标族）：

```text
X-RAG-Internal-Token        status=200 families=27
Authorization: Bearer       status=200 families=27
Bearer（错误 token）        status=403
不带鉴权                    status=403
```

另新增 7 例纯函数用例覆盖 token 提取：内部头优先、Bearer 前缀大小写不敏感、空 Bearer
不算 token、非 Bearer 的 Authorization 被忽略、两者皆无返回 None。

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| **监控栈本身** | 没有部署。`docker` 在本机不可用，且引入 Prometheus + Grafana 属于新增部署组件，按之前的约定需要先确认；这两个文件是 opt-in 的配置片段，不接进 compose 就不会生效 |
| Grafana 面板 JSON | 没写。面板文件绑定数据源 uid 与 schema 版本，栈还没定时写出来只能靠猜，容易变成"看起来能用但导入报错"的产物。栈定下来后按实际版本生成并不复杂 |
| 告警路由 | 只有规则，没有 Alertmanager 配置与通知渠道。这属于运维环境信息，不是本仓库能决定的事 |
| 服务内护栏与告警的重复 | 两处都定义了阈值（服务内 `evaluate_alerts` 与告警文件）。保留重复是有意的：前者在没有监控栈的环境里靠 `alert_count` 序列可见，后者提供触发历史与静默能力；交叉校验测试保证序列名不会漂移 |

## 6. 变更文件

```text
services/rag/monitoring/prometheus-scrape.example.yml   抓取配置（新增）
services/rag/monitoring/rag-tree-alerts.yml             告警规则（新增）
services/rag/app/routers/health.py                      /metrics 接受 Bearer + 常量时间比较
services/rag/tests/test_monitoring_config.py            配置与序列交叉校验（新增 4 例）
services/rag/tests/test_metrics_auth.py                 token 提取（新增 7 例）
services/rag/docs/树形RAG实施计划.md                    拆分"指标与看板"完成项与未完成项
```
