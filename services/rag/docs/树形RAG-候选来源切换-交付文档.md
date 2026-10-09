# 树形RAG 候选来源切换 交付文档

- **阶段**: Phase 2 补齐（Week 7 Day 3-4「memory_service 正则来源切到 LLM 来源」）
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-09
- **状态**: 正则来源默认关闭，窗口扫描成为唯一候选来源
- **关联文档**: 树形RAG实施计划.md (v2.5 §4.3 Week 7)、树形RAG-Phase2-窗口挂载与质量体系-交付文档.md

## 1. 为什么必须切

Phase 1 让正则发现充当过渡来源，Phase 2 的窗口扫描接手。但代码里两条路一直是**并行**
写同一个候选池的：

```text
memory_service（正则）   confidence 0.55   method=regex
window_scan（LLM）       模型置信度 0.85   method=llm
```

审核队列因此会混着两种质量的候选，而正则的准确率问题不是"偶尔不准"，是**成片错**：

```text
云启项目今天上线，另外签了采购合同
  -> [('云启项目', 'project'), ('另外签了采购合同', 'contract')]

关于2026年度采购管理办法的通知
  -> [('关于2026年度采购管理办法', 'policy')]

张三加入了蓝海科技有限公司
  -> [('张三加入了蓝海科技有限公司', 'organization')]
```

第三条最典型：正则把"主语+动词+公司全名"整段当成机构名，因为它只认 `2~40 个字 +
公司/项目/系统/合同/制度` 这个形状，没有边界意识。这类候选进了审核队列，审核人要
逐条判断并拒绝，正好抵消窗口扫描省下来的工作量。

线上数据也印证了两条路的实际产出：`entity_candidate_mentions` 里 277 条 mention、
93 个候选，`extraction_method` **全部是 llm**——正则那条路虽然接着，但没有产出可用
候选（有内容进来时才会暴露问题，属于早晚会踩的坑）。

## 2. 交付物

### 2.1 默认关闭正则来源

```text
RAG_MEMORY_REGEX_CANDIDATES_ENABLED   默认 false
```

`MemoryCandidateService.process` 在关闭时直接返回
`{"candidate_count": 0, "mention_count": 0, "skipped": 1}`，不再扫描 chunk、不再写
候选。lane 本身仍保留在 ingestion 流水线里（`parse → index → memory → callback`），
因为内存 lane 的位置和埋点是现成的，去掉它要动 job 状态机，收益不成比例。

### 2.2 为什么是开关而不是删代码

窗口扫描是定时跑的，新入库内容到下一次扫描之间有个窗口期。保留开关意味着：万一
扫描 worker 停摆，运维可以把正则来源临时打开兜住候选产出，而不必改代码重新发版。
默认值站在"质量"这一边，异常时才靠人工翻转——方向不能反过来。

## 3. 验收证据

```text
pytest -q   ->  200 passed, 4 skipped
```

新增用例（`tests/test_memory_candidates.py`）：

```text
默认关闭：即便内容里全是可被正则命中的形状，也不写任何候选
临时打开：过渡来源仍然可用，"云启项目" 能被发现
记录过捕获行为：捕获结果里存在明显长于真实实体名的条目（即"另外签了采购合同"
              这类整段误捕获），把它固化成一个可复现的证据
```

流水线回归（`tests/test_mvp_runtime.py`）：正式跑 `_run_memory` 的那条用例显式打开
开关，覆盖过渡路径；默认路径由上面第一条覆盖。

## 4. 未做与后续

| 项 | 说明 |
|---|---|
| 审核埋点 | Week 7 还有一项「审核时长、批准率、拒绝率埋点」未做。当前 `entity_review_requests` 为 0，没有审核行为可埋；等首批人工审核产生后再做，否则埋出来的是一条空曲线 |
| 历史脏候选 | 真实库里目前没有 `extraction_method='regex'` 的行，不需要清理；若其他环境有，按候选的 `extraction_method` 过滤即可 |
| 正则代码本身 | 保留未删。它是"过渡来源"的实现，删掉会让回退选项消失；但默认关闭，不会污染审核队列 |

## 5. 变更文件

```text
services/rag/app/application/memory_service.py   关闭正则候选写入
services/rag/app/config.py                       RAG_MEMORY_REGEX_CANDIDATES_ENABLED
services/rag/tests/test_memory_candidates.py     单测（新增 3 例）
services/rag/tests/test_mvp_runtime.py           过渡路径显式打开开关
services/rag/docs/树形RAG实施计划.md              勾选该项
```
