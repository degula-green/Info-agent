# 架构说明

当前项目采用三个业务微服务，并通过 Web 和 Nginx 对外提供能力：

```text
apps/web                   Vue 3 + TypeScript 前端
gateway/nginx              统一入口与路径转发
services/core              Go + Gin IAM 与权限服务
services/knowledge         Go + Gin 采集与知识管理服务
services/rag               Python + FastAPI RAG 服务
agents/wechat-collector    本地 Python 个人微信采集端
```

`agents/wechat-collector` 属于模块二的边缘采集组件，不是第四个业务微服务。它只在用户电脑上读取微信数据并调用 Knowledge Service，不直接访问 PostgreSQL、Redis、MinIO、OpenFGA 或 Elasticsearch。

请求路径：

```text
/api/core/*      -> core-service:8080
/api/knowledge/* -> knowledge-service:8090
/api/rag/*       -> rag-service:8000
```

本地基础设施：PostgreSQL、Redis、MinIO、Elasticsearch、OpenFGA。

数据职责：Core 拥有身份、组织和权限，Knowledge 拥有会话、消息、附件和知识对象，RAG 只拥有解析结果及检索索引。PostgreSQL 保存业务数据，MinIO 保存原始文件，Elasticsearch 保存全文和向量索引，Redis 保存缓存与任务状态，OpenFGA 保存资源权限关系。
