# 树形RAG 提取结果缓存与限流 交付文档

- **阶段**: Phase 2 补齐（Week 6 Day 3-4「结果缓存与限流」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 缓存与限流完成并实测；限流默认关闭（实测没有触发需求）
- **关联文档**: 树形RAG实施计划.md (v2.5 §4.3 Week 6)、树形RAG-Phase2-窗口挂载与质量体系-交付文档.md、树形RAG-窗口扫描性能实测-交付文档.md

## 1. 为什么这个缓存不是"顺手加的"

`_run_windows` 的注释写得很清楚：**第一个失败的窗口会中止整批，水位线不动**——
这是对的，半条会话不能算处理完。但代价是下一轮扫描会把**整条会话的窗口全部重抽**，
包括那些已经成功、已经付过钱的窗口。模型调用是整个扫描里最贵的部分（实测单窗口
1.3~6.5s、几百 token），失败一次就重复付一遍。

所以「结果缓存」在这里要解决的不是理论上的重复，而是**已存在的重试放大**。

## 2. 交付物

### 2.1 结果缓存（`app/application/extraction_cache.py`）

```text
键       sha256(prompt)
值       解析后的 JSON payload（深拷贝存、深拷贝取）
淘汰     LRU，上限 RAG_EXTRACT_CACHE_SIZE（默认 512）
并发     threading.Lock；窗口是并发跑的，缓存必须线程安全
统计     hits / misses / size / hit_rate
```

**只缓存成功的调用。** 异常直接往上抛，不进缓存——否则一次瞬时故障会被当成"模型对
这个窗口的答案"反复重放，等于把丢事实变成永久丢事实。空正文已经被抽取客户端转成
`ExtractionError`，所以在这一点上自动被覆盖。

取出来时深拷贝，是因为 payload 会交给 `clean_entities` / `clean_relations` 处理；
如果哪天那里改成原地修改，污染的就是之后每一次复用。

### 2.2 限流

```text
RAG_EXTRACT_MIN_INTERVAL_MS   两次调用开始之间的最小间隔，0 表示不限流（默认）
```

并发度约束的是"同时几个在飞"，限流约束的是"每秒发起几个"——后者才是多数供应商配额
的形状。两者互不替代，所以都留着。默认 0 是因为实测（并发 1/8/16 无排队、TPM 约
1.6万/9.4万/17万）没有出现被限流的迹象，只在需要时打开。

### 2.3 接线

`EntityWindowScanWorker._extract(prompt)` 统一走"查缓存 → 等限流 → 调用 → 写缓存"，
`_apply_window` 不再直接碰 `self.extractor`。一轮扫描真的省下调用时打一行 INFO
（`hits > 0` 才打，避免正常扫描刷日志）。

## 3. 前提：提示词必须可复现

缓存键是提示词，所以"同一窗口两次构建出的提示词完全一致"是这套设计成立的前提。
如果提示词里混进时间戳、集合遍历顺序之类的东西，缓存会永远不命中——而且**看起来
像在工作**（有缓存对象、有容量、有统计，只是 hit_rate 恒为 0）。

因此补了一条专门的断言：同一窗口构建两次（一次传 tuple、一次传 list）必须得到
完全相同的字符串。

## 4. 验收证据

```text
pytest -q   ->  197 passed, 4 skipped
```

重试去重（端到端，3 个窗口、第 3 个失败）：

```text
第一轮：3 个窗口全部尝试，第 3 个抛错 -> 会话失败、水位线不动
第二轮：重新应用 3 个窗口，其中 2 个命中缓存，只有失败那个重新调用
        extractor.calls 从 3 变成 4（没有缓存的话会是 6）
```

真实配置接线检查：

```text
cache_enabled = True | cache_size = 512 | min_interval_ms = 0
cache         = ExtractionCache | max_entries = 512
rate_limiter  = CallRateLimiter | interval = 0.0
同一提示词第二次调用 -> 命中，stats = {'hits': 1, 'misses': 1, 'hit_rate': 0.5}
```

新增用例：同提示词命中、不同提示词未命中、取出的副本被篡改不影响缓存、超界淘汰最旧、
被访问过的条目在淘汰中存活、空缓存命中率为 0、非 mapping 不入缓存、多线程并发填充
不丢条目、零间隔不等待、最小间隔确实拉开、提示词确定性、重试只付失败窗口。

## 5. 未做与后续

| 项 | 说明 |
|---|---|
| 缓存是进程内的 | 进程重启后失效。跨进程/持久化缓存（Redis 或 PG）需要新的失效策略和一次迁移，当前扫描是常驻 worker，重启后重扫本来也要发生，暂不引入 |
| 命中率未进指标 | 目前只在日志里；要做成时间序列需要给 `entity_scan_runs` 加列（一次迁移），等确认这个数字值得长期看再加 |
| 限流未启用 | 默认 0。实测没有触发供应商限流；如果后续上量到出现 429，把 `RAG_EXTRACT_MIN_INTERVAL_MS` 打开即可，不需要改代码 |

## 6. 变更文件

```text
services/rag/app/application/extraction_cache.py  缓存与限流（新增）
services/rag/app/application/window_scan_service.py 接入 _extract
services/rag/app/config.py                        三个新配置
services/rag/tests/test_extraction_cache.py       单测（新增 10 例）
services/rag/tests/test_window_scan_service.py    +2 例（重试去重、提示词确定性）
services/rag/docs/树形RAG实施计划.md              勾选该项
```
