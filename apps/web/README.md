# Info Agent 前端原型

这是 Info Agent 的纯前端交互原型，基于 Vue 3、TypeScript、Vite、Pinia 和 TDesign Vue Next。

## 启动

    npm install
    npm run dev

打开 http://localhost:5173，登录页已经预填演示账号。

## 原型边界

- 登录、搜索、知识库切换、会话接入、停止/继续采集、消息详情、附件预览和问答均使用本地 mock 数据。
- 编辑资料、编辑消息、问答历史和采集状态会保存在当前浏览器的 localStorage/sessionStorage。
- 不调用后端、数据库、飞书、企业微信、个人微信、采集服务或远程 API。
- npm run build 生成可直接部署到静态 Web 服务器的 dist 目录。
