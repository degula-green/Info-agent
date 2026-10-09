# 树形RAG 标注集与离线评估 - 交付文档

- **对应计划项**: 实施计划 Phase 2「评估集与指标看板」中的标注集部分；同时是 Phase 3 关系准确率与 Phase 4 自动化的前置条件
- **分支**: `codex/tree-rag-v2`
- **完成日期**: 2026-10-10
- **状态**: 框架与两个 suite 完成；mount suite 与看板未做（见第 5 节）

## 1. 为什么先做这个

前三个阶段的验收都卡在同一件事上：没有标注数据，准确率类的指标一个都测不出来。

```text
Phase 2 要求：挂载准确率 > 85%、人工批准率 > 80%
Phase 3 要求：关系提取准确率 > 80%
Phase 4 要求：自动合并准确率 > 80%、消歧 > 75%、自动批准 > 95%
```

这些都不能靠"跑一遍看着还行"来验收。而且 Phase 4 的自动化本身需要人工审核记录
作为监督信号——标注集不做，自动化就没有依据。

## 2. 交付物

### 2.1 数据模型（迁移）

| 文件 | 说明 |
|---|---|
| `db/migrations/20261011_tree_rag_eval.up.sql` | `eval_cases` + `eval_runs` |
| `db/migrations/20261011_tree_rag_eval.down.sql` | 回滚 |

已接入两处部署清单。

```text
eval_cases  id, scope, suite, dataset_version, query, labels(jsonb), notes,
            status(active|retired), created_by, timestamps
            UNIQUE(scope, suite, dataset_version, query)
eval_runs   id, scope, suite, dataset_version, case_count, passed_count,
            metrics(jsonb), label, created_at
```

**为什么用 version 而不是就地修改**：标注集必须冻结才能比较。改了 prompt、换了
模型、调了阈值之后，如果同一条 query 的金标准也跟着变，指标涨跌就归因不到任何
东西上。所以同一 query 在新版本里是一条新记录（`dataset_version` 递增），旧记录
保留，需要废弃时把 `status` 置为 `retired`。

### 2.2 指标计算（纯函数）

```text
evaluate_location_case(predicted, expected) -> (correct, recall, precision)
  精确集合相等才算正确——多定位一个实体不是"接近正确"，它会放宽检索范围、
  把无关 chunk 放进来。
  expected 为空是合法标签（"这里没有实体"），此时凭空定位出实体算失败。

summarize_location(outcomes)  -> case_count / exact_match_rate /
                                 mean_recall / mean_precision
evaluate_retrieval_case(ranked, gold, k) -> (correct, recall@k, reciprocal_rank)
summarize_retrieval(outcomes) -> case_count / recall_at_k / mrr
```

### 2.3 运行服务（`app/application/tree_eval_service.py`）

```text
TreeEvalService.import_cases(...)        导入标注集，按 (suite, version, query) 幂等
TreeEvalService.run_entity_location(...) 跑定位 suite，记录 eval_run
TreeEvalService.run_retrieval(...)       跑检索 suite，记录 eval_run
```

两个 run 都把失败样本的前 20 条写进 `metrics.failures`——只看一个百分比无法知道
该改哪一层，失败样本才是下一步动作的来源。

### 2.4 CLI 入口

```bash
# 导入（幂等）
python scripts/run_tree_eval.py import \
    --suite entity_location --version 1 \
    --scope-type organization --scope-id <uuid> --file cases.json

# 运行并记录
python scripts/run_tree_eval.py run \
    --suite entity_location --version 1 \
    --scope-type organization --scope-id <uuid> --label before-prompt-v2
```

JSON 格式：

```json
[{"query": "A项目的服务器配置",
  "labels": {"entity_ids": ["<entity-uuid>"]},
  "notes": "简称场景"}]
```

检索 suite 的标签键是 `chunk_ids`。脚本复用 `build_container()`，因此评测跑的是
与线上完全相同的装配（含定位层需要的 embedding provider）。

## 3. 验收证据

### 3.1 单元测试

```text
pytest: 134 passed, 4 skipped

新增 test_tree_eval_service.py（10 例）:
  - 精确集合相等才算对；多定位一个实体降 precision、少定位降 recall
  - "无实体"标签下凭空定位算失败
  - 空集合的汇总不崩且返回零
  - recall@k 与 MRR，金标准在 k 之外不计入
  - 导入按 (suite, version, query) 幂等，重复导入不产生重复行
  - 不同 dataset_version 互相隔离（同一 query 在两个版本可挂不同金标准）
  - 缺 query / 缺 labels 的畸形用例被跳过
  - 运行时记录 eval_run，含 label 与 passed_count
```

### 3.2 真实数据端到端

在真实组织 scope 建 `A项目`、`张三` 两个实体，导入 4 条用例（其中 1 条故意标错），
执行 CLI：

```text
$ python scripts/run_tree_eval.py import --suite entity_location --version 1 ...
{"imported": 4, "suite": "entity_location", "version": 1}

$ python scripts/run_tree_eval.py run --suite entity_location --version 1 --label baseline
{
  "case_count": 4,
  "exact_match_rate": 0.75,
  "mean_recall": 0.75,
  "mean_precision": 0.75,
  "failures": [{"query": "B项目的进展", "predicted": [], "expected": ["1a19f841-..."]}]
}

数据库：eval_runs = (entity_location, 1, 4, 3, 'baseline', 0.75)
```

故意标错的那条如期出现在 failures 里，说明失败样本可定位到具体 query。验证后
`eval_cases` / `eval_runs` / 测试实体均已清理。

## 4. 怎么用它做决策

```text
改 prompt / 换模型 / 调阈值
  -> 同一 dataset_version 重跑，label 写清变更内容
  -> 对比 eval_runs 的 metrics，而不是凭感觉
  -> 指标变差就回滚该变更，dataset_version 不用动

新增一批难点
  -> dataset_version + 1 导入，旧版本保持冻结
  -> 新旧版本的指标不可直接比较，因为题目变了
```

## 5. 未完成项

### 5.1 mount suite（窗口 -> 实体）

表结构已支持 `suite='mount'`，指标函数还没实现。它需要人工读完 20 条消息窗口才能
标注，成本最高，因此放在最后。可行的省力路径是先只标注"该窗口属于哪些实体"，
不标"不应属于哪些"，用召回优先的口径起步。

### 5.2 标注生产率

当前只有 JSON 导入，没有辅助标注的工具。规划中的两条省力路径：

```text
- 从真实消息反向构造查询：消息里本来就有实体名，把原句改写成查询比凭空编写更贴近真实分布
- LLM 预标注 + 人工修正，但标注模型不能与被测模型相同，否则是自证偏差
```

### 5.3 看板

`eval_runs` 已经逐次记录了指标，Grafana 面板未配置。

### 5.4 首批真实标注集

本交付只完成了框架与真实数据上的流程验证（4 条自造用例）。真正可用来验收的
定位集建议 50~100 条、检索集 50~100 条，需要人工投入才能产出。

## 6. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-10-10 | 标注集存储、指标计算、运行服务与 CLI 完成 |
