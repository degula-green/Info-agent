# 文档预处理与向量化任务

> 状态：实现基线。预处理、结构化切块和向量化已在 `services/rag` 落地；模块二事件/回源契约、Redis Stream 和 Elasticsearch 仍通过适配层对接。

本轮已确认：真实附件允许发送到 MinerU 云端和 Embedding 服务；生产使用云端精准解析 API；第一阶段 MinerU 文档统一直接使用 `vlm`；HTML、旧版 Office 和加密文档暂不支持。MinIO 短时 URL 的云端可达性尚未单独验证，初期默认准备“申请 MinerU 上传 URL并上传”的路径。

## 1. 目标与边界

本阶段完成：

- 取得并校验附件，按文件类别选择解析器；
- 使用 MinerU 解析 PDF、Office 和图片类文档；
- 保留阅读顺序、页码、标题、表格、公式、图片和代码等结构；
- 输出可复用的规范化解析产物、清单和质量结果；
- 按附件类型结构化切块；
- 调用 Embedding 服务并生成可校验的 ChunkRecord。

本阶段不在预处理适配层内自行实现：

- 模块二最终事件 Payload 和业务数据库读取；
- OpenFGA 授权判断、敏感等级判断或内容脱敏。

Redis Stream 消费、模块二 HTTP 回源、Elasticsearch 写入和混合检索已由 RAG 服务的独立适配层承载。

预处理只携带并校验 `content_access_required`、`content_version` 等上下文，不改变权限事实。允许解析不等于允许预览或下载。

## 2. 旧项目问题与本次修正

| 旧项目做法 | 本次规则 |
|---|---|
| 调用 `mineru.cli.common.do_parse/read_fn` 私有入口 | 通过 `MinerUClient` 封装公开 API；若以后本地部署，也只依赖公开 CLI/API 契约 |
| 把模型目录写入 `MINERU_MODEL_SOURCE` | 云端不设置该变量；本地只使用 `huggingface`、`modelscope` 或 `local` |
| 固定 `pipeline`，没有复杂文档降级 | 第一阶段对 MinerU 支持的文档直接使用 `vlm`；质量不合格进入复核，不自动切回另一模型 |
| 递归取第一个 Markdown 和第一组图片 | 读取 `full.md`、`*_content_list.json` 及全部资源，并校验引用 |
| 只保存一段 Markdown | Markdown 作为阅读回退，同时保存结构化 block 和解析清单 |
| 字符切块、表格和页面信息丢失 | 基于 Canonical block 按类型切块，使用 token 上限而非固定字符窗口 |
| 没有哈希、解压和结果完整性校验 | 预检、幂等键、安全解压、产物哈希和质量检查均为必需项 |
| 将完整解析文本复制到 PostgreSQL | 解析正文和资源只存 MinIO 派生产物，数据库保存引用、版本和质量摘要 |

旧项目参考：`D:/agentworkspace/info-agent/info-agent/services/rag/app/services/document_parser.py`、`vectorization.py`、`attachment_worker.py`。

## 3. 过渡期输入

模块二契约未定前，先用 Fixture 或本地命令调用同一条预处理流水线。输入适配器只需提供以下最小字段：

```json
{
  "attachment_id": "att_001",
  "file_path": "fixtures/input/report.pdf",
  "object_ref": null,
  "file_name": "report.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 123456,
  "source_content_hash": "sha256:...",
  "content_version": 1,
  "content_access_required": false,
  "acl_version": 3
}
```

生产环境再由 `document.processing.requested` 或等价事件映射到相同的内部输入，不让 MinerU 适配器依赖消息字段。

## 4. 文件类型路由

| 类别 | 第一阶段处理 | 说明 |
|---|---|---|
| `txt`、`md`、`json` | 轻量本地解析 | 规范化编码和换行；保留标题、段落和代码边界，不调用 MinerU |
| `csv`、`tsv` | 轻量表格解析 | 保留列名、行列关系和原始单元格，不压成不可逆的一行文本 |
| `pdf` | MinerU `vlm` | 第一阶段统一使用 VLM，覆盖文本型、扫描件、多栏、表格和公式 |
| `docx`、`pptx`、`xlsx` | MinerU `vlm` 原生解析 | 不先转换为 PDF，避免丢失 Office 结构 |
| `png`、`jpg`、`jpeg` | MinerU `vlm`/OCR | 以 OCR 文本和页面资源为主；图片本身作为 asset 保存 |
| `html`、`doc`、`ppt`、`xls` | 暂不支持 | 后续单独评估，不在第一阶段混入路由 |
| 压缩包、可执行文件、音视频、加密/密码文档 | 明确失败 | 不自动解压、不猜测密码、不进入后续索引 |

纯文本的“短/长”只影响后续切块策略：短文本可以作为一个 block，长文本保留段落和标题供后续切块；本阶段不按字符粗暴切块。

MinerU 云端单文件上限按官方当前限制预检：不超过 200 MB、200 页；项目配置可以设置更低的业务上限，超过限制直接失败。

## 5. MinerU 接入策略

### 5.1 适配层

```text
DocumentPreprocessor
  ├─ PreflightValidator
  ├─ ParserRouter
  ├─ MinerUClient
  │    ├─ submit_by_url
  │    ├─ submit_by_upload
  │    ├─ poll_task
  │    └─ download_result
  ├─ ResultUnpacker
  ├─ ArtifactNormalizer
  └─ QualityChecker
```

优先使用 MinerU 精准解析 API（Bearer Token、异步任务），SDK 仅作为官方客户端/HTTP 封装放在适配器内部，云端 v4 REST 契约作为稳定边界；业务层只依赖上述适配器，不直接导入 MinerU 私有函数。Token 只从运行环境读取，不写入代码、Fixture、日志或产物。

### 5.2 文件提交

本轮采用 MinerU 云端精准解析 API。MinIO 短时 URL 尚未验证可达性，因此初期默认调用 `POST /api/v4/file-urls/batch` 获取 MinerU 上传 URL并上传文件；验证 URL 可达后，才将 `POST /api/v4/extract/task` 的 URL 提交作为优化路径。

不把长期 MinIO 地址、访问密钥或下载令牌传给下游；URL 设置最短可用 TTL，并使用 HTTPS。

MinerU 精准 API 当前单文件限制约为 200 MB、200 页；超过限制在预检阶段失败，不在本阶段静默拆分。批量提交可作为吞吐优化，初期先保证单文件链路可重试和可观测。

### 5.3 模型 profile

| profile | 使用场景 | 默认动作 |
|---|---|---|
| `vlm` | 第一阶段全部 MinerU 支持的 PDF、Office 和图片 | 默认且唯一的 MinerU profile |
| `pipeline` | 后续吞吐/成本优化 | 第一阶段不启用 |
| `MinerU-HTML` | HTML | 第一阶段不支持 |

默认打开表格、公式和必要的图片分析；解析语言、页面范围和 profile 写入 `parser_options`，纳入幂等指纹。VLM 质量不合格时标记 `needs_review` 或 `failed`，不在第一阶段自动切换到 `pipeline`。

### 5.4 异步任务

提交后保存 MinerU `task_id` 和内部 `run_id`，轮询 `waiting-file`、`pending`、`running`、`converting`、`done`、`failed` 等状态。轮询使用指数退避、最大等待时间和明确的超时状态；网络错误、429、5xx 可重试，格式不支持、超限、密码保护等直接失败。完成后下载 `full_zip_url`，不在事件中传输正文或二进制。

官方示例 `opendatalab/MinerU/demo/demo.py` 采用“提交 → 等待 → 下载 ZIP → `safe_extract_zip`”模式，本项目沿用其异步和安全解压思路。

## 6. 端到端预处理流程

```text
Fixture / object_ref
→ 读取元数据并校验扩展名、MIME、文件魔数、大小、哈希
→ ParserRouter 选择轻量解析或 MinerU `vlm`
→ （MinerU）生成短时 URL或上传文件 → 提交异步任务
→ 轮询任务状态并下载结果 ZIP
→ 校验 ZIP，安全解压并定位完整结果
→ 读取 full.md + content_list.json，按需保留 middle/model JSON
→ 规范化页面、标题、段落、列表、表格、公式、图片、代码
→ 校验内容、页数、资源引用和结构质量
→ 保存 artifact 与 manifest，返回 preparse 结果
→ 按附件类型切块，生成 ChunkRecord
→ 批量调用 Embedding，校验向量后保存 chunk 产物
→ 后续模块再写入 ES 并执行混合检索
```

VLM 结果质量不达标时标记 `needs_review` 或 `failed`，不把低质量文本静默交给后续 RAG。后续如需启用 `pipeline`，必须新增 profile 版本并重新做样本基准。

## 7. 规范化产物

建议以不可变版本保存到 MinIO 私有派生目录；Fixture 阶段可保存到本地未入库目录。轻量文本解析也必须输出同一套 Canonical block 和 manifest，不能让下游为本地文件另写一条格式分支：

```text
<attachment_id>/<content_version>/<run_id>/
  manifest.json
  full.md
  content_list.json
  middle.json       # 有则保留
  model.json        # 有则保留
  assets/
    images/
    tables/
    formulas/
```

不要假定文件名固定；按结果包内的后缀和关联关系定位文件。`full.md` 用于人工阅读和回退，`content_list.json` 用于阅读顺序和 block 结构，图片/表格/公式资源必须全部校验并保留引用。

### 7.1 Canonical block

下游只依赖统一 block，不再直接解析 MinerU 原始 JSON：

```json
{
  "page_number": 2,
  "order": 7,
  "type": "text|title|list|table|equation|image|code",
  "text": "...",
  "html": "...",
  "latex": "...",
  "asset_ref": "assets/images/figure-1.png",
  "bbox": [0, 0, 0, 0],
  "heading_path": ["章节", "小节"]
}
```

字段按类型取值：表格保留 HTML 和可检索文本，公式保留 LaTeX，图片保留资源引用、页码和可用的描述；不把二进制内嵌到 JSON，不在预处理阶段把表格拆成独立无上下文行。

### 7.2 Manifest 最小字段

```text
artifact_schema_version
attachment_id / content_version / acl_version
source_content_hash
file_name / mime_type / size_bytes / page_count
parser / parser_version / model_version / backend
parser_options_hash
artifact_hash
block_count / asset_count
status / quality_flags
created_at
```

原始文件不复制到解析产物目录；需要回源时使用模块二提供的对象引用。原文、解析结果和图片均按业务安全级别存放，禁止写入日志。

### 7.3 正式存储位置与旧项目差异

正式流程建议如下：

```text
模块二 MinIO：原始附件（source object）
→ 模块三临时目录：下载/上传/解压，任务结束清理
→ 模块三 MinIO：不可变解析产物（derived artifact）
→ rag.processing_jobs：保存 parsed_artifact_ref、parsed_content_hash、page_count 和版本
→ 后续 ES：只保存切块、全文字段和向量，不保存整包解析产物
```

派生路径至少包含 `attachment_id/content_version/run_id/artifact_hash`，不同解析器、模型或选项不得覆盖旧产物。Redis 只保存任务状态和事件引用，不保存正文或二进制。

上一个项目的 `app/services/object_store.py` 通过 `derived_prefix(context)` 生成 `user/platform/conversation/message/derived/attachment/parser_version` 前缀，并上传 `document.md`、`canonical.json` 和 `assets/`。随后 `attachment_worker.py` 调用 `pgvector_store.py` 的 `upsert_attachment_document`，把完整解析文本复制到 `vector_store.documents.content`，元数据保存对象 key，后续再写入 PG/ES 向量。该做法可作为迁移参考，但 V3 采用 MinIO 派生产物 + 数据库引用，避免正文重复、版本覆盖和结构丢失。

## 8. 切块与向量化任务

### 8.1 公共流程

```text
Canonical blocks
→ 按类型合并为逻辑段
→ 继承标题路径、文件/Sheet/页码和 ACL 版本
→ 生成 embedding_text = 标题路径 + 标题 + 内容
→ 批量调用 Embedding 服务
→ 校验返回数量、模型和向量维度
→ 输出 ChunkRecord，等待 ES 写入
```

正文按 token 切分，不按固定字符截断。初始建议目标 `500~800 tokens`、硬上限 `1000~1200 tokens`；普通正文重叠 `50~100 tokens`，表格、公式、幻灯片和图片不重叠。最终值以 `text-embedding-v4` 服务限制和样本召回评测为准。

### 8.2 类型化切块规则

| 类型 | 切块规则 | 向量化内容 |
|---|---|---|
| TXT/MD | 标题 → 段落/列表/代码块；短文本一个 chunk | 标题路径 + 正文；代码保留语言和边界 |
| JSON | 对象路径、对象或数组窗口 | key/path + 规范化值，保留结构上下文 |
| PDF/DOCX | 章节和段落；页码只作定位 | 标题路径 + 段落；表格/公式单独成块或与说明合并 |
| PPTX | 默认一页一个 chunk；超长页在页内拆分 | 幻灯片标题 + 正文 + 备注 + 图表说明 |
| XLSX/CSV | Sheet/表格为范围；大表按行窗口并重复表头 | Sheet 名 + 表头 + 行组；精确数值依赖 BM25 |
| 图片 | OCR 文本、标题或 VLM 描述；无文本只保留 asset | 只向量化文字，不向量化图片二进制 |

表格优先保持完整；超过上限时按连续行分组，保留表名、列名、行范围和上下文。公式保留 LaTeX，图片保留 `asset_ref` 和页面定位。

### 8.3 ChunkRecord 与权限

每个 ChunkRecord 至少包含：

```text
chunk_id / chunk_index / chunk_count / chunking_version
knowledge_item_id / attachment_id / content_version / acl_version
part_kind / chunk_type / title / content / source_locator
content_hash / embedding_model / embedding / rag_eligible
```

`content_access_required=true` 的附件原文及向量只能进入受保护索引；Embedding 服务已允许接收此类原文，但检索时仍必须先按 OpenFGA 授权对象过滤，再执行 BM25/kNN。无有效文本的图片或元数据块设置 `rag_eligible=false`。

### 8.4 向量化校验与性能

- 按同一模型批量请求，避免一次性物化全部 chunk；失败批次可重试。
- 每个响应校验数量、顺序、模型名和维度；与 `EMBEDDING_DIMS` 不一致直接失败。
- 相同 `content_hash + embedding_model` 可复用向量；模型或维度变化必须提升版本并重建。
- 先保存 ChunkRecord/处理状态，再交给 ES bulk 写入；ES 写入失败不得标记任务完成。

## 9. 校验、幂等与失败恢复

- **预检**：扩展名、MIME、魔数、文件大小、页数（可读取时）、哈希和空文件检查；扩展名与魔数不一致直接失败。
- **ZIP 安全**：禁止路径穿越、绝对路径、符号链接和嵌套炸弹；限制文件数、单文件解压大小和总解压大小。
- **结果完整性**：至少存在非空 `full.md` 或可转出的 block；`content_list` 顺序合法；页码/资源引用可解析；表格 HTML、公式 LaTeX 和图片文件不为空。
- **质量标记**：记录空页比例、文本量、重复页眉页脚、乱码/替换字符、无引用资源和疑似阅读顺序异常；结果为 `passed`、`needs_review` 或 `failed`。
- **幂等键**：`source_content_hash + parser_version + model_version + parser_options_hash`；同一指纹已有成功产物时跳过重新解析。
- **阶段状态**：`preflight → submitted → processing → downloaded → normalized → checked → succeeded/needs_review/failed`。
- **可恢复性**：下载成功但规范化失败时只重跑规范化；只有无可复用结果或 profile 改变时才重新调用 MinerU。
- **安全边界**：本轮已允许受保护附件发送到 MinerU 云端；仍必须使用短时 URL或一次性上传地址，原样传递资源和 ACL 版本，并禁止正文进入日志。

## 10. 开发阶段与验收

| 阶段 | 内容 | 结束条件 |
|---|---|---|
| A | 固定云端 API、VLM、支持格式和限额；建立合成或已获批准的 Fixture 集 | 样本和决策项冻结 |
| B | Fixture 输入、预检和类型路由 | 每种支持格式有明确结果和错误码 |
| C | `MinerUClient`、异步轮询、URL/上传两种提交路径 | 可用假服务测试提交、轮询、超时和重试 |
| D | ZIP 安全处理、结构化规范化、artifact/manifest | Markdown、JSON、资源和引用可复现 |
| E | 类型化切块、Embedding 批处理和向量校验 | 每种附件产出稳定 ChunkRecord |
| F | 质量评分、复核标记、幂等和性能指标 | 低质量结果不会静默进入下游 |
| G | 预处理/向量化基准与交接 | 形成 ES 写入和混合检索所需契约 |

第一批 Fixture 至少覆盖：中文短文本、长 Markdown、数字 PDF、扫描 PDF、含多栏/表格/公式的 PDF、DOCX、PPTX、XLSX、图片、损坏文件、超限文件、扩展名伪装文件和密码文档。验收关注结构完整性、资源引用正确率、重复解析命中率、成功率、P50/P95 延迟和 MinerU 配额消耗。

## 11. 尚待确认事项

1. MinIO 短时 URL 是否能被 MinerU 云端直接访问；未验证前按 `file-urls/batch` 上传路径实现。
2. 正式派生 bucket/prefix、产物保留期限、访问审计和失败样本保留策略。
3. 后续 `document.extracted` 需要返回完整 artifact 引用、质量摘要，还是只返回处理状态；这不阻塞当前 Fixture 开发，但需在正式事件契约中确定。

## 参考资料

- [MinerU API 文档](https://mineru.net/apiManage/docs)
- [MinerU 输出文件说明](https://opendatalab.github.io/MinerU/reference/output_files/)
- [MinerU 快速使用](https://opendatalab.github.io/MinerU/usage/quick_usage/)
- [MinerU 官方 API 示例](https://github.com/opendatalab/MinerU/blob/master/demo/demo.py)
