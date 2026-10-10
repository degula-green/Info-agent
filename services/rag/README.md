# RAG service

FastAPI API plus a Redis Streams worker implementing the no-compatibility
`rag_mvp` pipeline.

## Runtime

```powershell
uv sync
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run python worker.py
```

The process reads `services/rag/.env`. Existing process environment variables
take precedence.

## Storage contract

- RAG-owned PostgreSQL schema: `RAG_DATABASE_SCHEMA=rag_mvp`.
- Chunk projections: `rag_chunks_display_v1` and
  `rag_chunks_protected_v1`.
- Read aliases: `rag_chunks_display_read` and
  `rag_chunks_protected_read`.
- Write aliases: `rag_chunks_display_write` and
  `rag_chunks_protected_write`.
- Vectors live only in Elasticsearch. PostgreSQL stores chunk text,
  embedding metadata, branches and projection state.
- Old `rag.memory_*` tables and old Elasticsearch indices are not read or
  written by the MVP runtime; their data is intentionally left untouched.

Apply migrations from the repository root before starting the worker.

## Processing flow

```text
knowledge.ready
  -> Dispatcher: persist processing_jobs, then ACK Redis
  -> Parse Lane: fetch source, parse, chunk, persist rag_mvp.chunks
  -> Index Lane: Embedding, controlled Entity matching, branch_keys, ES bulk
  -> ready/metadata_only callback through rag_mvp.outbox_events
  -> Memory Lane: candidate Entity discovery only
```

Each Lane uses a bounded in-process queue. PostgreSQL jobs and leases are the
authoritative recovery source when a process or queue is lost.

## Retrieval

The public API is under `/api/v1`:

- `/search/global`: BM25 + kNN + RRF.
- `/search/knowledge`: BM25 + kNN + RRF.
- `/search/keyword`: BM25 only. It does not call the embedding provider, kNN,
  tree location, or attachment-neighbour expansion.
- `/search/tree`: entity-scoped retrieval (`off`, `shadow`, or `tree`).
- `/ai/documents` and `/ai/documents/stream`: RAG-backed answer generation.

`shadow` computes tree diagnostics while returning the traditional result.
`tree` returns only the entity-scoped result, or an empty result with a reason;
the caller decides whether to request traditional retrieval next.
The deployment default is now `tree`, so every organization can use the tree
surface. Set `RAG_TREE_MODE=shadow` or `off` to roll back without deleting tree
data.

## Boundaries

- RAG does not read Knowledge or Core databases.
- Knowledge content is fetched through internal HTTP APIs.
- Authorization is delegated to Core through `search-scope` and
  `check-batch`; protected branches fail closed.
- Controller/API adapters may accept the old frontend request shape, but
  storage and domain code only use the frozen MVP contract.
