"""Runtime configuration for the RAG service.

The service deliberately reads environment variables directly. The local
launcher imports ``services/rag/.env`` before starting the process and
production deployments inject the same variables through the container
runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = _text("RAG_ENV", "development")
    service_name: str = _text("RAG_SERVICE_NAME", "rag")
    http_host: str = _text("RAG_HTTP_HOST", "0.0.0.0")
    http_port: int = _int("RAG_HTTP_PORT", 8000)
    log_level: str = _text("RAG_LOG_LEVEL", "INFO")
    internal_auth_token: str = _text("RAG_INTERNAL_AUTH_TOKEN")

    # RAG-owned PostgreSQL state (jobs, index records, search history, QA and
    # the RAG outbox). Knowledge/IAM tables remain owned by their services.
    database_url: str = _text("RAG_DATABASE_URL")
    database_schema: str = _text("RAG_DATABASE_SCHEMA", "rag")
    database_min_pool_size: int = _int("RAG_DATABASE_MIN_POOL_SIZE", 1)
    database_max_pool_size: int = _int("RAG_DATABASE_MAX_POOL_SIZE", 10)
    database_connect_timeout_seconds: float = _float(
        "RAG_DATABASE_CONNECT_TIMEOUT_SECONDS", 5.0
    )
    database_command_timeout_seconds: float = _float(
        "RAG_DATABASE_COMMAND_TIMEOUT_SECONDS", 10.0
    )

    # Elasticsearch 9.x. Username/password and API key are alternatives.
    elasticsearch_url: str = _text("ELASTICSEARCH_URL", "http://127.0.0.1:9200")
    elasticsearch_username: str = _text("ELASTICSEARCH_USERNAME")
    elasticsearch_password: str = _text("ELASTICSEARCH_PASSWORD")
    elasticsearch_api_key: str = _text("ELASTICSEARCH_API_KEY")
    elasticsearch_ca_cert_path: str = _text("ELASTICSEARCH_CA_CERT_PATH")
    elasticsearch_display_index: str = _text(
        "ELASTICSEARCH_DISPLAY_INDEX", _text("ELASTICSEARCH_INDEX", "knowledge_display_chunks_read")
    )
    elasticsearch_protected_index: str = _text(
        "ELASTICSEARCH_PROTECTED_INDEX", "knowledge_protected_chunks_read"
    )
    memory_display_facts_index: str = _text("MEMORY_DISPLAY_FACTS_INDEX", "memory_display_facts_read")
    memory_protected_facts_index: str = _text("MEMORY_PROTECTED_FACTS_INDEX", "memory_protected_facts_read")
    memory_display_nodes_index: str = _text("MEMORY_DISPLAY_NODES_INDEX", "memory_display_nodes_read")
    memory_protected_nodes_index: str = _text("MEMORY_PROTECTED_NODES_INDEX", "memory_protected_nodes_read")
    elasticsearch_verify_certs: bool = _bool("ELASTICSEARCH_VERIFY_CERTS", False)
    elasticsearch_connect_timeout_seconds: float = _float(
        "ELASTICSEARCH_CONNECT_TIMEOUT_SECONDS", 0.3
    )
    elasticsearch_request_timeout_seconds: float = _float(
        "ELASTICSEARCH_REQUEST_TIMEOUT_SECONDS", 1.5
    )
    elasticsearch_max_retries: int = _int("ELASTICSEARCH_MAX_RETRIES", 2)
    elasticsearch_retry_on_timeout: bool = _bool(
        "ELASTICSEARCH_RETRY_ON_TIMEOUT", True
    )
    elasticsearch_max_connections: int = _int("ELASTICSEARCH_MAX_CONNECTIONS", 50)

    # Embedding model and dimensions are part of the physical ES index contract.
    embedding_api_base_url: str = _text("EMBEDDING_API_BASE_URL")
    embedding_api_key: str = _text("EMBEDDING_API_KEY")
    embedding_model: str = _text("EMBEDDING_MODEL", "text-embedding-v4")
    embedding_dims: int = _int("EMBEDDING_DIMS", 1536)
    embedding_connect_timeout_seconds: float = _float(
        "EMBEDDING_CONNECT_TIMEOUT_SECONDS", 1.0
    )
    embedding_timeout_seconds: float = _float("EMBEDDING_TIMEOUT_SECONDS", 5.0)
    embedding_batch_size: int = _int("EMBEDDING_BATCH_SIZE", 32)
    embedding_max_retries: int = _int("EMBEDDING_MAX_RETRIES", 2)
    embedding_cache_enabled: bool = _bool("EMBEDDING_CACHE_ENABLED", True)
    embedding_cache_ttl_seconds: int = _int("EMBEDDING_CACHE_TTL_SECONDS", 3600)

    # MinerU cloud precise API.
    mineru_api_base_url: str = _text("MINERU_API_BASE_URL", "https://mineru.net/api/v4")
    mineru_api_token: str = _text("MINERU_API_TOKEN")
    mineru_model_version: str = _text("MINERU_MODEL_VERSION", "vlm")
    mineru_submit_mode: str = _text("MINERU_SUBMIT_MODE", "upload")
    mineru_enable_ocr: bool = _bool("MINERU_ENABLE_OCR", True)
    mineru_enable_formula: bool = _bool("MINERU_ENABLE_FORMULA", True)
    mineru_enable_table: bool = _bool("MINERU_ENABLE_TABLE", True)
    mineru_language: str = _text("MINERU_LANGUAGE", "ch")
    mineru_no_cache: bool = _bool("MINERU_NO_CACHE", False)
    mineru_cache_tolerance_seconds: int = _int(
        "MINERU_CACHE_TOLERANCE_SECONDS", 900
    )
    mineru_http_timeout_seconds: float = _float("MINERU_HTTP_TIMEOUT_SECONDS", 60.0)
    mineru_result_download_timeout_seconds: float = _float(
        "MINERU_RESULT_DOWNLOAD_TIMEOUT_SECONDS", 300.0
    )
    mineru_poll_interval_seconds: float = _float("MINERU_POLL_INTERVAL_SECONDS", 5.0)
    mineru_task_max_wait_seconds: float = _float(
        "MINERU_TASK_MAX_WAIT_SECONDS", 1800.0
    )
    mineru_max_retries: int = _int("MINERU_MAX_RETRIES", 3)

    # MinIO source and derived artifacts.
    minio_endpoint: str = _text("RAG_MINIO_ENDPOINT")
    minio_access_key: str = _text("RAG_MINIO_ACCESS_KEY")
    minio_secret_key: str = _text("RAG_MINIO_SECRET_KEY")
    minio_secure: bool = _bool("RAG_MINIO_SECURE", True)
    minio_source_bucket: str = _text("RAG_MINIO_SOURCE_BUCKET", "info-agent")
    minio_derived_bucket: str = _text(
        "RAG_MINIO_DERIVED_BUCKET", "info-agent-rag-derived"
    )
    minio_derived_prefix: str = _text("RAG_MINIO_DERIVED_PREFIX", "parsed")
    minio_presigned_url_ttl_seconds: int = _int(
        "RAG_MINIO_PRESIGNED_URL_TTL_SECONDS", 900
    )
    artifact_retention_days: int = _int("RAG_ARTIFACT_RETENTION_DAYS", 0)

    # Preprocessing safety limits.
    preprocess_work_dir: Path = Path(
        _text("RAG_PREPROCESS_WORK_DIR", ".runtime/preprocess")
    )
    preprocess_max_file_bytes: int = _int(
        "RAG_PREPROCESS_MAX_FILE_BYTES", 209715200
    )
    preprocess_max_pages: int = _int("RAG_PREPROCESS_MAX_PAGES", 200)
    preprocess_max_unpack_files: int = _int(
        "RAG_PREPROCESS_MAX_UNPACK_FILES", 10000
    )
    preprocess_max_unpack_bytes: int = _int(
        "RAG_PREPROCESS_MAX_UNPACK_BYTES", 1073741824
    )

    # Service 1 is the only OpenFGA owner. RAG talks to its HTTP API only.
    authz_base_url: str = _text("RAG_AUTHZ_BASE_URL")
    authz_api_token: str = _text("RAG_AUTHZ_API_TOKEN")
    authz_caller_service: str = _text("RAG_AUTHZ_CALLER_SERVICE", "rag")
    authz_connect_timeout_seconds: float = _float(
        "RAG_AUTHZ_CONNECT_TIMEOUT_SECONDS", 0.2
    )
    authz_timeout_seconds: float = _float("RAG_AUTHZ_TIMEOUT_SECONDS", 0.8)
    authz_scope_cache_ttl_seconds: int = _int(
        "RAG_AUTHZ_SCOPE_CACHE_TTL_SECONDS", 30
    )
    authz_scope_max_objects: int = _int("RAG_AUTHZ_SCOPE_MAX_OBJECTS", 10000)

    # Service 2 is used by workers for HTTP source backfill, not normal ES
    # search requests.
    knowledge_base_url: str = _text("RAG_KNOWLEDGE_BASE_URL")
    knowledge_api_token: str = _text("RAG_KNOWLEDGE_API_TOKEN")
    knowledge_connect_timeout_seconds: float = _float(
        "RAG_KNOWLEDGE_CONNECT_TIMEOUT_SECONDS", 2.0
    )
    knowledge_timeout_seconds: float = _float("RAG_KNOWLEDGE_TIMEOUT_SECONDS", 10.0)
    knowledge_callback_enabled: bool = _bool("RAG_KNOWLEDGE_CALLBACK_ENABLED", True)
    knowledge_callback_path: str = _text("RAG_KNOWLEDGE_CALLBACK_PATH", "/internal/knowledge/{knowledge_item_id}/rag-result")

    # Redis Streams.
    redis_url: str = _text("RAG_REDIS_URL")
    redis_database: int = _int("RAG_REDIS_DATABASE", 3)
    redis_username: str = _text("RAG_REDIS_USERNAME")
    redis_password: str = _text("RAG_REDIS_PASSWORD")
    redis_tls: bool = _bool("RAG_REDIS_TLS", False)
    redis_inbound_stream: str = _text("RAG_REDIS_INBOUND_STREAM")
    redis_consumer_group: str = _text("RAG_REDIS_CONSUMER_GROUP", "rag-workers")
    redis_consumer_name: str = _text("RAG_REDIS_CONSUMER_NAME")
    redis_outbound_stream: str = _text("RAG_REDIS_OUTBOUND_STREAM")
    redis_dlq_stream: str = _text("RAG_REDIS_DLQ_STREAM")
    redis_block_ms: int = _int("RAG_REDIS_BLOCK_MS", 5000)
    redis_batch_size: int = _int("RAG_REDIS_BATCH_SIZE", 10)
    redis_max_retries: int = _int("RAG_REDIS_MAX_RETRIES", 3)
    redis_claim_idle_ms: int = _int("RAG_REDIS_CLAIM_IDLE_MS", 60000)

    # QA and optional reranking providers.
    qa_api_base_url: str = _text("QA_API_BASE_URL")
    qa_api_key: str = _text("QA_API_KEY")
    qa_model: str = _text("QA_MODEL")
    qa_connect_timeout_seconds: float = _float("QA_CONNECT_TIMEOUT_SECONDS", 2.0)
    qa_timeout_seconds: float = _float("QA_TIMEOUT_SECONDS", 45.0)
    qa_stream: bool = _bool("QA_STREAM", True)
    qa_max_context_tokens: int = _int("QA_MAX_CONTEXT_TOKENS", 6000)
    qa_max_chunks: int = _int("QA_MAX_CHUNKS", 8)
    qa_max_output_tokens: int = _int("QA_MAX_OUTPUT_TOKENS", 1200)
    memory_extraction_version: str = _text("RAG_MEMORY_EXTRACTION_VERSION", "v1")
    memory_tree_strategy_version: str = _text("RAG_MEMORY_TREE_STRATEGY_VERSION", "v1")
    memory_summary_strategy_version: str = _text("RAG_MEMORY_SUMMARY_STRATEGY_VERSION", "v1")

    rerank_enabled: bool = _bool("RERANK_ENABLED", False)
    rerank_api_base_url: str = _text("RERANK_API_BASE_URL")
    rerank_api_key: str = _text("RERANK_API_KEY")
    rerank_model: str = _text("RERANK_MODEL")
    rerank_timeout_seconds: float = _float("RERANK_TIMEOUT_SECONDS", 0.8)
    rerank_top_n: int = _int("RERANK_TOP_N", 12)

    # Retrieval and indexing defaults.
    search_deadline_ms: int = _int("RAG_SEARCH_DEADLINE_MS", 1500)
    es_query_timeout_ms: int = _int("RAG_ES_QUERY_TIMEOUT_MS", 500)
    bm25_top_k: int = _int("RAG_BM25_TOP_K", 24)
    knn_top_k: int = _int("RAG_KNN_TOP_K", 24)
    knn_num_candidates: int = _int("RAG_KNN_NUM_CANDIDATES", 96)
    rrf_k: int = _int("RAG_RRF_K", 60)
    rrf_keyword_weight: float = _float("RAG_RRF_KEYWORD_WEIGHT", 0.5)
    rrf_vector_weight: float = _float("RAG_RRF_VECTOR_WEIGHT", 0.5)
    final_top_k: int = _int("RAG_FINAL_TOP_K", 8)
    max_chunks_per_item: int = _int("RAG_MAX_CHUNKS_PER_ITEM", 2)
    query_rewrite_enabled: bool = _bool("RAG_QUERY_REWRITE_ENABLED", False)
    query_rewrite_max: int = _int("RAG_QUERY_REWRITE_MAX", 1)
    highlight_final_only: bool = _bool("RAG_HIGHLIGHT_FINAL_ONLY", True)

    chunking_version: str = _text("RAG_CHUNKING_VERSION", "v1")
    chunk_max_tokens: int = _int("RAG_CHUNK_MAX_TOKENS", 512)
    chunk_overlap_tokens: int = _int("RAG_CHUNK_OVERLAP_TOKENS", 64)
    index_bulk_size: int = _int("RAG_INDEX_BULK_SIZE", 100)
    index_flush_interval_ms: int = _int("RAG_INDEX_FLUSH_INTERVAL_MS", 500)
    index_max_retries: int = _int("RAG_INDEX_MAX_RETRIES", 3)

    worker_concurrency: int = _int("RAG_WORKER_CONCURRENCY", 4)
    worker_prefetch: int = _int("RAG_WORKER_PREFETCH", 10)
    task_max_retries: int = _int("RAG_TASK_MAX_RETRIES", 3)
    task_visibility_timeout_seconds: int = _int(
        "RAG_TASK_VISIBILITY_TIMEOUT_SECONDS", 900
    )
    worker_shutdown_timeout_seconds: int = _int(
        "RAG_WORKER_SHUTDOWN_TIMEOUT_SECONDS", 30
    )

    otel_enabled: bool = _bool("RAG_OTEL_ENABLED", False)
    otel_exporter_otlp_endpoint: str = _text("RAG_OTEL_EXPORTER_OTLP_ENDPOINT")
    metrics_enabled: bool = _bool("RAG_METRICS_ENABLED", True)

    @property
    def elasticsearch_index(self) -> str:
        """Legacy single-index name; callers should use display/protected fields."""
        return self.elasticsearch_display_index


settings = Settings()
