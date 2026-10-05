# Scripts

## 启动开发环境

如果只需要演示 Info Agent 前端，请执行：

    .\scripts\start-frontend.ps1

它只启动 `apps/web` 的 Vite 开发服务器，不启动 Core、RAG、数据库或 Nginx。

可以直接双击：

```text
scripts/start-platform.cmd
```

也可以在项目根目录执行：

```powershell
.\scripts\start-dev.ps1
```

脚本会启动九个进程：Vue、Gin Core、Gin Knowledge、FastAPI RAG、FastAPI Agent、WeChat Collector、
Agent Knowledge Worker、Agent Task Worker 和 Nginx。首次运行会自动执行 `npm ci`，并用 `uv sync` 分别创建和同步
RAG、WeChat Collector、Agent 的 `.venv`。

## 同步 OpenFGA 删除权限模型

删除组织消息需要 OpenFGA 模型包含 `organization#information_admin` 和
`knowledge_item#moderator/delete`。如果 Core 的 `check-batch` 返回
`AUTHZ_BACKEND_UNAVAILABLE`，而 OpenFGA `/healthz` 正常，通常是远端 store
仍在使用旧模型。执行：

```powershell
python .\scripts\sync-openfga-model.py
```

脚本会基于 store 当前模型补充缺失的删除关系，创建新模型，并把新的
`CORE_OPENFGA_MODEL_ID` 写回 `services\core\.env`。同步后重启 Core。

其中 Agent 的三个进程是本项目的决策中枢：

| 进程 | 作用 |
| --- | --- |
| `agent` | HTTP API，端口取 `services\agent\.env` 的 `AGENT_HTTP_PORT`（当前 8095） |
| `agent-knowledge-worker` | 消费 Knowledge 的 `knowledge:ready` 流，把采集到的消息扇出成 Task |
| `agent-task-worker` | 消费 `agent:tasks`，负责规划、执行、审批与重规划 |

少了后两个 worker，采集到的消息会一直堆在 Redis 里，Agent 不会产生任何 Task。启动脚本会在重启前
按 Agent 解释器路径清理这三个进程，因此不会误杀 RAG 的 `worker.py`。

个人微信采集器由同一启动脚本启动，运行于用户本机并读取用户提供的微信数据库目录。应用当前按单机单实例运行。

需要先安装：Node.js、Go、uv。uv 会选择兼容的 Python 3.11/3.12，并管理各服务的虚拟环境。未安装 uv 时脚本会跳过同步、
直接复用已有的 `.venv`（前提是这些虚拟环境已经建好）。Nginx 未安装时，Core、Knowledge、RAG、Web、WeChat Collector
和 Agent 三个进程仍会启动。
