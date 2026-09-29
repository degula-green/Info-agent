# 06 RAG 内部端口

> 状态：已冻结

## 1. 目标

内部端口用于隔离业务逻辑和基础设施，不暴露 HTTP，不使用旧 `memory_*` 实现。

## 2. Repository Ports

### TaskRepository

```text
create_or_get_job(event) -> Job
get_job(job_id) -> Job
claim_jobs(lane, limit, lease_seconds) -> list[Job]
list_recoverable_jobs(now) -> list[Job]
heartbeat(job_id, lease_seconds)
update_stage(job_id, stage)
complete_job(job_id, status)
fail_job(job_id, error, retryable)
```

### SourceSnapshotRepository

```text
upsert_snapshot(context) -> ResourceSnapshot
get_snapshot(snapshot_id) -> ResourceSnapshot
get_by_knowledge_item(knowledge_item_id, content_version)
list_active_by_scope(scope)
```

### ChunkRepository

```text
upsert_chunks(snapshot_id, chunks)
list_chunks(snapshot_id, variant=None)
list_chunks_by_ids(chunk_ids)
update_embedding_status(chunk_ids, status, model, dimensions)
```

### OutboxRepository

```text
add_event(event)
pending_events(limit)
mark_published(event_id)
mark_failed(event_id, error)
```

### EntityRegistryRepository

```text
upsert_entity(entity)
upsert_aliases(entity_id, aliases)
list_entities(scope)
get_entity(entity_id)
list_aliases(entity_id)
delete_alias(alias_id)
submit_candidate(candidate)
list_candidates(scope, filters)
get_candidate(candidate_id)
list_candidate_mentions(candidate_id)
review_candidate(candidate_id, action, target_entity_id, reviewer_id, review_request_id, expected_status)
```

### BranchRepository

```text
replace_chunk_branches(chunk_id, branches)
resolve_branch_keys(scope, entity_ids, time_range)
create_refresh_job(entity_id, registry_version)
list_refresh_jobs(status)
get_refresh_job(job_id)
get_tree(scope, filters)
get_tree_node(node_id)
```

### ProjectionRepository

```text
upsert_projection(record)
mark_indexed(chunk_id, variant, mapping_version)
mark_failed(chunk_id, variant, mapping_version, error)
```

## 3. Infrastructure Ports

### KnowledgeSource

```text
get_knowledge(knowledge_item_id, content_version, acl_version)
get_content(knowledge_item_id, content_version, acl_version, variant)
get_attachment(attachment_id, content_version, acl_version)
```

### EmbeddingProvider

```text
embed(texts) -> vectors
model
dimensions
```

### SearchIndexer

```text
index_chunks(chunks)
delete_older_versions(resource_id, content_version)
search_bm25(request)
search_knn(request)
```

### AuthorizationGateway

```text
search_scope(user_id, scope, resource_parts)
check_batch(user_id, scope, checks, snapshot_id)
```

### CallbackPublisher

```text
publish_status(payload)
```

## 4. 测试约束

- 业务逻辑只能依赖端口，不直接依赖 psycopg、Elasticsearch、Redis 或 Knowledge HTTP 实现。
- 单元测试可以使用内存实现。
- 集成测试必须使用 `rag_mvp` 和新 ES 索引。
- 旧 `memory_*` Repository 不注册到新依赖容器。
