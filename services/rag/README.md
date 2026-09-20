# RAG service

使用 `uv` 管理独立的 `.venv`、依赖锁文件和运行命令。

```powershell
uv sync
uv run python -m uvicorn app.main:app --reload --port 8000
```

通常直接双击 `../../scripts/start-platform.cmd` 即可；依赖缺失或
`pyproject.toml` 或 `uv.lock` 发生变化时，启动脚本会自动同步环境。

直接启动 `app.main:app` 或 `worker.py` 时，入口会读取
`services/rag/.env`（已存在的进程环境变量优先）。

RAG 自有 PostgreSQL 表使用 `RAG_DATABASE_URL` 和 `RAG_DATABASE_SCHEMA=rag`，
启动前先应用仓库根目录 `db/migrations`。API 进程只提供检索入口；Redis
Streams 处理任务单独运行：

```powershell
uv run python worker.py
```

模块二的业务表仍只能通过 HTTP 回源，RAG 不直接读取；服务一负责 OpenFGA，
RAG 只调用服务一的授权 API。
