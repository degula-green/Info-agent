# 架构说明

当前项目采用三个业务微服务、一个本机采集服务，并通过 Web 和 Nginx 对外提供能力：

```text
apps/web                   Vue 3 + TypeScript 前端
gateway/nginx              统一入口与路径转发
services/core              Go + Gin IAM 与权限服务
services/knowledge         Go + Gin 采集与知识管理服务
services/rag               Python + FastAPI RAG 服务
services/collectors/wechat 本机 Python 个人微信采集服务
```

`services/collectors/wechat` 属于模块二的本机采集组件，不是业务微服务。它运行在用户电脑上读取微信数据库，并调用 Knowledge Service 的受控内部接口，不直接访问 PostgreSQL、Redis、MinIO、OpenFGA 或 Elasticsearch。应用默认按单机单实例运行；如果 Knowledge 等服务部署到远程环境，Collector 仍需在用户本机运行并通过网络访问 Knowledge。

请求路径：

```text
/api/core/*      -> core-service:8080
/api/knowledge/* -> knowledge-service:8090
/api/rag/*       -> rag-service:8000
```

本地基础设施：PostgreSQL、Redis、MinIO、Elasticsearch、OpenFGA。

数据职责：Core 拥有身份、组织和权限，Knowledge 拥有会话、消息、附件和知识对象，RAG 只拥有解析结果及检索索引。PostgreSQL 保存业务数据，MinIO 保存原始文件，Elasticsearch 保存全文和向量索引，Redis 保存缓存与任务状态，OpenFGA 保存资源权限关系。
