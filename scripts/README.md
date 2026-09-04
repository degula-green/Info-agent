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

脚本会启动五个进程：Vue、Gin Core、Gin Knowledge、FastAPI RAG 和 Nginx。首次运行会自动执行
`npm ci`，并用 `uv` 将 Python 依赖安装到 `services/rag/.runtime/python`；该目录不是虚拟环境。

本地微信采集端不会由该脚本自动启动。需要采集个人微信时，应在用户确认后从 `agents/wechat-collector` 单独启动。

需要先安装：Node.js、Go、系统 Python、uv。Nginx 未安装时，其他四个服务仍会启动。
