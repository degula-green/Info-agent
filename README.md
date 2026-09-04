# info-agent

信息管理 Agent 项目。当前先以 Web 形态开发，后续可复用 Vue 前端接入 Electron。

```text
info-agent/
├── apps/
│   └── web/                 # Vue 3 + TypeScript 前端
├── services/
│   ├── core/                # Go + Gin IAM 与权限服务
│   ├── knowledge/           # Go + Gin 采集与知识管理服务
│   └── rag/                 # Python + FastAPI RAG 服务
├── agents/
│   └── wechat-collector/    # 本地 Python 个人微信采集端
├── gateway/
│   └── nginx/               # Nginx 网关配置
├── db/
│   ├── migrations/          # 数据库迁移文件
│   └── seeds/               # 初始化数据
├── docker/                  # 服务器部署用 Docker 文件
├── scripts/                 # 开发、构建、启动脚本
└── docs/                    # 项目文档
```

## 技术架构

```text
Vue Web
  ↓
Nginx 网关 :80
  ├── /api/core/*      → core-service      :8080
  ├── /api/knowledge/* → knowledge-service :8090
  └── /api/rag/*       → rag-service       :8000
```

### 前端

- Vue 3：页面和组件开发。
- TypeScript：类型约束。
- Vite：开发服务器和构建工具。
- 后续使用 Electron 作为桌面端壳，复用 `apps/web`。

### 后端

- `services/core`：Go + Gin，负责用户、组织、身份认证和权限。
- `services/knowledge`：Go + Gin，负责连接器编排、消息采集、会话、附件和知识对象管理。
- `services/rag`：Python + FastAPI，负责文本处理、Embedding、Elasticsearch 检索和 AI 问答。
- `agents/wechat-collector`：在用户电脑上运行的 Python 采集端，未来负责读取个人微信数据并上报 Knowledge Service，不独立拥有业务数据。
- 三个服务可以独立部署，通过 Nginx 按路径转发；微信 Agent 不加入服务器端部署。

### 基础设施

```text
PostgreSQL       业务数据和结构化信息
Redis            缓存、任务状态和队列
MinIO            原始文件、附件和图片
OpenFGA          用户与资源之间的权限关系
Elasticsearch    全文检索、向量检索和混合检索
```

## 本地启动

### 纯前端演示（推荐）

Info Agent 页面可以完全脱离后端独立运行：

    .\scripts\start-frontend.ps1

也可以进入 `apps/web` 后执行 `npm install` 和 `npm run dev`。

打开 `http://localhost:5173`。登录、搜索、知识库切换、接入会话、停止/继续采集、消息详情、附件预览和 AI 问答均使用本地 mock 数据，不会请求任何后端、数据库、飞书、微信或采集服务。

### 全平台开发环境（保留）

双击：`scripts/start-platform.cmd`

或在命令行执行：

```powershell
./scripts/start-dev.ps1
```

首次启动会自动恢复前端与 RAG Python 依赖。统一检索入口为
`GET /api/rag/search?q=关键词`，Nginx 会将其转发到 RAG 服务的 `/search`。
