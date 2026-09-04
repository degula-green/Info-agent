# WeChat collector agent

模块二的本地 Python 采集端骨架。它将在用户电脑上读取个人微信数据，并通过 Knowledge Service 的受控接口上报。

当前版本不会加载微信数据库、不会启动轮询，也不会发送任何数据。

## 配置

```text
KNOWLEDGE_BASE_URL=http://127.0.0.1:8090
WECHAT_ID=
WECHAT_DATABASE_DIR=
```

## 手动运行

```powershell
python -m app.main
```

请在本目录执行命令。该 Agent 不加入服务器端 Docker Compose，也不随平台开发脚本自动启动。
