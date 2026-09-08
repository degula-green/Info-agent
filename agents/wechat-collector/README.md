# WeChat collector agent

模块二的本地 Python 采集端。它在用户电脑上校验并读取个人微信数据库，通过 Knowledge Service 的受控接口完成配对、会话发现、消息/附件上报、heartbeat 和游标提交。

Agent 会在本机校验并读取微信数据库，只向 Knowledge 发送规范化会话、消息和附件数据。完整数据库路径、设备密钥和对象存储凭据不会上传；路径只以 SHA-256 fingerprint 参与配对。

Agent 只支持 Windows，并固定使用已验证的 `wechatauto-replica==1.1.10.2`。安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 配置

```text
KNOWLEDGE_BASE_URL=http://127.0.0.1:8090/api/knowledge/v1
PAIRING_ID=
PAIRING_CODE=
WECHAT_ID=
WECHAT_DATABASE_DIR=
WECHAT_STATE_FILE=%LOCALAPPDATA%\\InfoAgent\\wechat-agent-state.json
WECHAT_AGENT_VERSION=wechat-collector/1.0
WECHAT_POLL_INTERVAL=10
WECHAT_DISCOVERY_INTERVAL=30
WECHAT_MAX_ATTACHMENT_BYTES=104857600
```

首次运行需要 `PAIRING_ID`、`PAIRING_CODE`、`WECHAT_ID` 和 `WECHAT_DATABASE_DIR`。配对成功后设备密钥会写入本地 state 文件（权限限制为当前用户），后续运行可使用该文件恢复。附件以流式 multipart 上传，Knowledge 成功提交 cursor 后 Agent 才保存本地 checkpoint。

## 手动运行

```powershell
python -m app.main
```

请在本目录执行命令。该 Agent 不加入服务器端 Docker Compose，也不随平台开发脚本自动启动。
