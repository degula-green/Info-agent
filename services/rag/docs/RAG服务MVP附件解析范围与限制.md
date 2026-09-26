# RAG 服务 MVP 附件解析范围与限制

> 状态：已确认设计基线
>
> 范围：`services/rag` 消费 `knowledge.ready` 后的附件处理
>
> 原则：格式白名单、大小硬限制、先校验后解析、不支持的附件跳过正文解析

## 1. 处理级别

附件分为四类：

```text
本地解析
MinerU 云端解析
metadata_only
failed
```

| 处理级别 | 含义 |
|---|---|
| 本地解析 | 使用 RAG 进程内代码直接解析 |
| MinerU 云端解析 | 上传文件或提交 URL 给 MinerU 云端 |
| `metadata_only` | 保留元数据，跳过正文解析，不生成 Chunk 和向量 |
| `failed` | 支持解析但发生临时故障，允许重试 |

## 2. 支持格式

### 2.1 本地解析

| 扩展名 | MIME | 解析方式 | 说明 |
|---|---|---|---|
| `txt` | `text/plain` | Python 标准库 | 普通纯文本 |
| `md` | `text/markdown` | Python 标准库 | Markdown |
| `json` | `application/json` | Python 标准库 | 必须为合法 JSON |
| `csv` | `text/csv` | Python 标准库 | 转为表格结构化 Chunk |
| `tsv` | `text/tab-separated-values` | Python 标准库 | 转为表格结构化 Chunk |

本地解析不依赖模型或外部解析服务。

### 2.2 MinerU 云端解析

| 扩展名 | MIME | 说明 |
|---|---|---|
| `pdf` | `application/pdf` | 支持文本 PDF 和 OCR PDF |
| `docx` | OOXML Word | MinerU 为主，失败时可使用本地 DOCX 降级解析 |
| `pptx` | OOXML PowerPoint | 依赖 MinerU |
| `xlsx` | OOXML Excel | 依赖 MinerU |
| `png` | `image/png` | 依赖 OCR |
| `jpg` | `image/jpeg` | 依赖 OCR |
| `jpeg` | `image/jpeg` | 依赖 OCR |

## 3. 不支持格式

以下格式一律跳过正文解析：

| 类别 | 扩展名或情况 |
|---|---|
| 旧版 Office | `doc`、`ppt`、`xls` |
| HTML | `html`、`htm` |
| 压缩包 | `zip`、`7z`、`rar` |
| 加密文件 | 密码保护 PDF、加密 Office、加密附件 |
| 音视频 | `mp3`、`mp4`、`wav`、`avi`、`mov` |
| 可执行文件 | `exe` 及其他二进制程序 |
| 其他图片 | `webp`、`heic`、`tiff`、`svg` |
| 未知格式 | 扩展名不在白名单中 |

处理结果：

```text
processing_status = metadata_only
skip_reason = unsupported_format | encrypted
```

不支持的附件：

- 不下发给 MinerU；
- 不创建 Chunk；
- 不调用 Embedding；
- 不生成 `branch_keys`；
- 不写入 Elasticsearch；
- 只保留 Knowledge 元数据和对象引用。

## 4. 文件大小限制

当前全局上限为 200 MiB。MVP 建议降低为 100 MiB。

### 4.1 全局硬上限

```text
RAG_PREPROCESS_MAX_FILE_BYTES=104857600
```

| 项目 | 上限 |
|---|---:|
| 单附件全局硬上限 | 100 MiB |

超过全局上限：

```text
processing_status = metadata_only
skip_reason = file_too_large
```

### 4.2 按类型限制

| 类型 | 单文件上限 | 其他限制 |
|---|---:|---|
| `txt`、`md` | 10 MiB | 无 |
| `json` | 10 MiB | 必须为合法 JSON |
| `csv`、`tsv` | 20 MiB | 不按行数继续部分解析 |
| `docx` | 50 MiB | 无页数预检 |
| `pptx` | 50 MiB | 最多 200 页或幻灯片 |
| `xlsx` | 50 MiB | 后续可增加工作表、行数和单元格限制 |
| `pdf` | 100 MiB | 最多 200 页 |
| `png`、`jpg`、`jpeg` | 20 MiB | 单图片 |

建议增加配置：

```text
RAG_PREPROCESS_MAX_TEXT_BYTES=10485760
RAG_PREPROCESS_MAX_TABLE_BYTES=20971520
RAG_PREPROCESS_MAX_OFFICE_BYTES=52428800
RAG_PREPROCESS_MAX_PDF_BYTES=104857600
RAG_PREPROCESS_MAX_IMAGE_BYTES=20971520
RAG_PREPROCESS_MAX_PAGES=200
RAG_PREPROCESS_MAX_SLIDES=200
```

## 5. 校验时机

### 5.1 Dispatcher 阶段

如果 Knowledge 元数据已经提供文件名和大小，Dispatcher 可以在下载前执行第一层判断：

```text
扩展名不支持 -> metadata_only
文件大小超限 -> metadata_only
附件已加密 -> metadata_only
```

这样可以避免下载无效大文件，减少网络和解析开销。

### 5.2 Preflight 阶段

实际文件下载后必须再次校验：

- 文件是否存在且非空；
- 文件实际大小；
- 文件扩展名；
- MIME 与扩展名是否匹配；
- 文件魔数；
- Office Open XML 包结构；
- PDF 是否加密；
- PDF 页数是否超限；
- 来源 content hash 是否一致；
- 来源 size_bytes 是否一致。

## 6. `metadata_only` 规则

以下情况进入 `metadata_only`：

```text
unsupported_format
file_too_large
password_protected
encrypted_attachment
unknown_format
page_limit_exceeded
slide_limit_exceeded
```

规则：

- `metadata_only` 是终态，不重试。
- 不生成 Chunk。
- 不生成 Embedding。
- 不写入 Elasticsearch。
- 不进入正式树。
- 不影响其他资源处理和检索。
- Knowledge 可以通过元数据目录展示文件名称、类型、大小和来源。

## 7. `failed` 规则

以下情况进入可重试失败：

```text
MinerU 临时不可用
MinIO 下载失败
网络超时
解析结果下载失败
Embedding 服务暂时不可用
Elasticsearch 暂时不可用
```

处理：

```text
processing_status = failed
retryable = true
```

达到重试上限后进入最终失败，不自动转为 `metadata_only`。

## 8. MinerU 结果安全限制

MinerU 返回 ZIP 时继续执行：

```text
RAG_PREPROCESS_MAX_UNPACK_FILES=10000
RAG_PREPROCESS_MAX_UNPACK_BYTES=1073741824
```

这些限制用于防止结果包解压炸弹和异常文件数量，与用户上传附件上限分开。

## 9. 处理状态映射

| 情况 | 状态 | 是否进入 ES |
|---|---|---|
| 格式支持，解析成功 | `ready` | 是 |
| 格式不支持 | `metadata_only` | 否 |
| 文件过大 | `metadata_only` | 否 |
| 加密或密码保护 | `metadata_only` | 否 |
| 页数或幻灯片超限 | `metadata_only` | 否 |
| 解析服务临时失败 | `failed`，可重试 | 否 |
| 超过重试上限 | `failed`，终态 | 否 |

## 10. 附件处理流程

```text
knowledge.ready
  -> Dispatcher 读取元数据
  -> 扩展名和元数据大小预检
  -> 不支持或超限：metadata_only，直接结束
  -> 支持：下载附件
  -> Preflight 实际文件校验
  -> 不支持或超限：metadata_only
  -> 支持：本地解析或提交 MinerU
  -> 生成 Chunk
  -> Embedding
  -> 写入 ES
  -> ready
```

## 11. 生产配置建议

```text
RAG_PREPROCESS_MAX_FILE_BYTES=104857600
RAG_PREPROCESS_MAX_TEXT_BYTES=10485760
RAG_PREPROCESS_MAX_TABLE_BYTES=20971520
RAG_PREPROCESS_MAX_OFFICE_BYTES=52428800
RAG_PREPROCESS_MAX_PDF_BYTES=104857600
RAG_PREPROCESS_MAX_IMAGE_BYTES=20971520
RAG_PREPROCESS_MAX_PAGES=200
RAG_PREPROCESS_MAX_SLIDES=200
RAG_PREPROCESS_MAX_UNPACK_FILES=10000
RAG_PREPROCESS_MAX_UNPACK_BYTES=1073741824
```
