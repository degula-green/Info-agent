# RAG service

使用系统 Python，不创建虚拟环境。依赖由 `uv` 管理。

```powershell
uv pip install --python "C:\Program Files\Python311\python.exe" --target .runtime/python --link-mode copy -r requirements.txt
$env:PYTHONPATH = "$PWD\.runtime\python"
python -m uvicorn app.main:app --reload --port 8000
```

通常直接双击 `../../scripts/start-platform.cmd` 即可；依赖缺失或
`requirements.txt` 发生变化时，启动脚本会自动执行上述安装。

直接启动 `app.main:app` 或 `worker.py` 时，入口会读取
`services/rag/.env`（已存在的进程环境变量优先）。

RAG 自有 PostgreSQL 表使用 `RAG_DATABASE_URL` 和 `RAG_DATABASE_SCHEMA=rag`，
启动前先应用仓库根目录 `db/migrations`。API 进程只提供检索入口；Redis
Streams 处理任务单独运行：

```powershell
python worker.py
```

模块二的业务表仍只能通过 HTTP 回源，RAG 不直接读取；服务一负责 OpenFGA，
RAG 只调用服务一的授权 API。
