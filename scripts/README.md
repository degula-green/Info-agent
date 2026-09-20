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

脚本会启动六个进程：Vue、Gin Core、Gin Knowledge、FastAPI RAG、WeChat Collector 和 Nginx。首次运行会自动执行
`npm ci`，并用 `uv sync` 分别创建和同步 RAG、WeChat Collector 的 `.venv`。

个人微信采集器由同一启动脚本启动，运行于用户本机并读取用户提供的微信数据库目录。应用当前按单机单实例运行。

需要先安装：Node.js、Go、uv。uv 会选择兼容的 Python 3.11/3.12，并管理各服务的虚拟环境。Nginx 未安装时，Core、Knowledge、RAG、Web 和 WeChat Collector 仍会启动。
