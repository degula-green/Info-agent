"""Runtime configuration for the Agent service.

The service reads environment variables directly, matching ``services/rag``.
Local development loads ``services/agent/.env`` from the process entry points;
deployments inject the same variables through the container runtime.

Existing keys in the local ``.env`` use a lowercase ``agent_`` prefix, so every
lookup accepts both ``AGENT_*`` and ``agent_*`` spellings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_KNOWLEDGE_PLATFORMS: tuple[str, ...] = ("feishu", "wecom", "wechat")


def _raw(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _text(default: str, *names: str) -> str:
    value = _raw(*names)
    return default if value is None else value


def _int(default: int, *names: str) -> int:
    value = _raw(*names)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{names[0]} must be an integer") from exc


def _float(default: float, *names: str) -> float:
    value = _raw(*names)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{names[0]} must be a number") from exc


def _bool(default: bool, *names: str) -> bool:
    value = _raw(*names)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = _text("development", "AGENT_ENV", "agent_ENV")
    service_name: str = _text("agent", "AGENT_SERVICE_NAME", "agent_SERVICE_NAME")
    log_level: str = _text("INFO", "AGENT_LOG_LEVEL", "agent_LOG_LEVEL")

    http_host: str = _text("0.0.0.0", "AGENT_HTTP_HOST", "agent_HTTP_HOST")
    http_port: int = _int(8080, "AGENT_HTTP_PORT", "agent_HTTP_PORT")
    jwt_public_key_file: str = _text(
        "",
        "AGENT_JWT_PUBLIC_KEY_FILE",
        "agent_JWT_PUBLIC_KEY_FILE",
    )
    jwt_issuer: str = _text(
        "info-agent-core",
        "AGENT_JWT_ISSUER",
        "agent_JWT_ISSUER",
        "CORE_JWT_ISSUER",
    )
    jwt_audience: str = _text(
        "info-agent-api",
        "AGENT_JWT_AUDIENCE",
        "agent_JWT_AUDIENCE",
        "CORE_JWT_AUDIENCE",
    )

    # PostgreSQL owns the authoritative Agent runtime state.
    database_url: str = _text("", "AGENT_DATABASE_URL", "agent_DATABASE_URL")
    database_schema: str = _text(
        "agent", "AGENT_DATABASE_SCHEMA", "agent_DATABASE_SCHEMA"
    )
    database_min_pool_size: int = _int(
        1, "AGENT_DATABASE_MIN_POOL_SIZE", "agent_DATABASE_MIN_POOL_SIZE"
    )
    database_max_pool_size: int = _int(
        10, "AGENT_DATABASE_MAX_POOL_SIZE", "agent_DATABASE_MAX_POOL_SIZE"
    )
    database_connect_timeout_seconds: float = _float(
        5.0, "AGENT_DATABASE_CONNECT_TIMEOUT_SECONDS", "agent_DATABASE_CONNECT_TIMEOUT_SECONDS"
    )
    database_command_timeout_seconds: float = _float(
        10.0, "AGENT_DATABASE_COMMAND_TIMEOUT_SECONDS", "agent_DATABASE_COMMAND_TIMEOUT_SECONDS"
    )

    # Redis only carries queue and wake-up signals.
    redis_url: str = _text("", "AGENT_REDIS_URL", "agent_REDIS_URL")
    redis_database: int = _int(0, "AGENT_REDIS_DATABASE", "agent_REDIS_DATABASE")
    redis_username: str = _text("", "AGENT_REDIS_USERNAME", "agent_REDIS_USERNAME")
    redis_password: str = _text("", "AGENT_REDIS_PASSWORD", "agent_REDIS_PASSWORD")
    redis_tls: bool = _bool(False, "AGENT_REDIS_TLS", "agent_REDIS_TLS")
    redis_inbound_stream: str = _text(
        "agent:tasks", "AGENT_REDIS_INBOUND_STREAM", "agent_REDIS_INBOUND_STREAM"
    )
    redis_outbound_stream: str = _text(
        "agent:events", "AGENT_REDIS_OUTBOUND_STREAM", "agent_REDIS_OUTBOUND_STREAM"
    )
    redis_consumer_group: str = _text(
        "agent-workers", "AGENT_REDIS_CONSUMER_GROUP", "agent_REDIS_CONSUMER_GROUP"
    )
    redis_consumer_name: str = _text(
        "", "AGENT_REDIS_CONSUMER_NAME", "agent_REDIS_CONSUMER_NAME"
    )
    redis_block_ms: int = _int(5000, "AGENT_REDIS_BLOCK_MS", "agent_REDIS_BLOCK_MS")
    redis_batch_size: int = _int(10, "AGENT_REDIS_BATCH_SIZE", "agent_REDIS_BATCH_SIZE")
    redis_claim_idle_ms: int = _int(
        60000, "AGENT_REDIS_CLAIM_IDLE_MS", "agent_REDIS_CLAIM_IDLE_MS"
    )

    # Knowledge owns conversation visibility and the platform tokens; the Agent
    # only calls its internal API, and only for the conversation snapshot.
    knowledge_base_url: str = _text(
        "", "AGENT_KNOWLEDGE_BASE_URL", "agent_KNOWLEDGE_BASE_URL"
    )
    knowledge_service_token: str = _text(
        "", "AGENT_KNOWLEDGE_SERVICE_TOKEN", "agent_KNOWLEDGE_SERVICE_TOKEN"
    )
    knowledge_timeout_seconds: float = _float(
        5.0, "AGENT_KNOWLEDGE_TIMEOUT_SECONDS", "agent_KNOWLEDGE_TIMEOUT_SECONDS"
    )
    knowledge_platforms: str = _text(
        ",".join(DEFAULT_KNOWLEDGE_PLATFORMS),
        "AGENT_KNOWLEDGE_PLATFORMS",
        "agent_KNOWLEDGE_PLATFORMS",
    )
    knowledge_ready_stream: str = _text(
        "knowledge:ready", "AGENT_KNOWLEDGE_READY_STREAM", "agent_KNOWLEDGE_READY_STREAM"
    )
    knowledge_consumer_group: str = _text(
        "agent-workers", "AGENT_KNOWLEDGE_CONSUMER_GROUP", "agent_KNOWLEDGE_CONSUMER_GROUP"
    )
    core_base_url: str = _text("", "AGENT_CORE_BASE_URL", "agent_CORE_BASE_URL")
    core_timeout_seconds: float = _float(
        5.0, "AGENT_CORE_TIMEOUT_SECONDS", "agent_CORE_TIMEOUT_SECONDS"
    )
    rag_base_url: str = _text("", "AGENT_RAG_BASE_URL", "agent_RAG_BASE_URL")
    rag_service_token: str = _text(
        "", "AGENT_RAG_SERVICE_TOKEN", "agent_RAG_SERVICE_TOKEN"
    )
    rag_timeout_seconds: float = _float(
        30.0, "AGENT_RAG_TIMEOUT_SECONDS", "agent_RAG_TIMEOUT_SECONDS"
    )
    rag_agent_tools_enabled: bool = _bool(
        False,
        "AGENT_RAG_AGENT_TOOLS_ENABLED",
        "agent_RAG_AGENT_TOOLS_ENABLED",
        "RAG_AGENT_METADATA_TOOLS_ENABLED",
        "rag_AGENT_METADATA_TOOLS_ENABLED",
    )
    default_timezone: str = _text(
        "Asia/Shanghai", "AGENT_DEFAULT_TIMEZONE", "agent_DEFAULT_TIMEZONE"
    )
    # Semantic understanding is opt-in. ``shadow`` records model output without
    # changing planning, so it can be enabled before ``enforce``.
    understanding_mode: str = _text(
        "off", "AGENT_UNDERSTANDING_MODE", "agent_UNDERSTANDING_MODE"
    )
    understanding_provider: str = _text(
        "rules", "AGENT_UNDERSTANDING_PROVIDER", "agent_UNDERSTANDING_PROVIDER"
    )
    # Which fast classifier hybrid runs before falling back to the LLM.
    understanding_primary: str = _text(
        "laya", "AGENT_UNDERSTANDING_PRIMARY", "agent_UNDERSTANDING_PRIMARY"
    )
    planner_provider: str = _text(
        "deterministic", "AGENT_PLANNER_PROVIDER", "agent_PLANNER_PROVIDER"
    )
    llm_base_url: str = _text(
        "", "AGENT_LLM_BASE_URL", "agent_LLM_BASE_URL"
    )
    llm_api_key: str = _text(
        "", "AGENT_LLM_API_KEY", "agent_LLM_API_KEY"
    )
    llm_model: str = _text(
        "", "AGENT_LLM_MODEL", "agent_LLM_MODEL"
    )
    llm_timeout_seconds: float = _float(
        10.0, "AGENT_LLM_TIMEOUT_SECONDS", "agent_LLM_TIMEOUT_SECONDS"
    )
    llm_max_output_tokens: int = _int(
        300, "AGENT_LLM_MAX_OUTPUT_TOKENS", "agent_LLM_MAX_OUTPUT_TOKENS"
    )
    llm_response_format: str = _text(
        "json_object", "AGENT_LLM_RESPONSE_FORMAT", "agent_LLM_RESPONSE_FORMAT"
    )
    # The Planner is the one caller that can hand the model a real JSON Schema,
    # so it gets its own switch: understanding and answer.compose still ask for
    # plain JSON objects. When a provider rejects the schema the client drops
    # back to json_object instead of failing the Task.
    llm_planner_response_format: str = _text(
        "json_schema",
        "AGENT_LLM_PLANNER_RESPONSE_FORMAT",
        "agent_LLM_PLANNER_RESPONSE_FORMAT",
    )
    llm_json_schema_fallback: bool = _bool(
        True, "AGENT_LLM_JSON_SCHEMA_FALLBACK", "agent_LLM_JSON_SCHEMA_FALLBACK"
    )
    laya_base_url: str = _text(
        "", "AGENT_LAYAYA_BASE_URL", "agent_LAYAYA_BASE_URL"
    )
    laya_api_key: str = _text(
        "", "AGENT_LAYAYA_API_KEY", "agent_LAYAYA_API_KEY"
    )
    laya_model: str = _text(
        "multilingual", "AGENT_LAYAYA_MODEL", "agent_LAYAYA_MODEL"
    )
    laya_model_path: str = _text(
        "", "AGENT_LAYAYA_MODEL_PATH", "agent_LAYAYA_MODEL_PATH"
    )
    laya_timeout_seconds: float = _float(
        5.0, "AGENT_LAYAYA_TIMEOUT_SECONDS", "agent_LAYAYA_TIMEOUT_SECONDS"
    )
    laya_min_confidence: float = _float(
        0.80,
        "AGENT_LAYAYA_MIN_CONFIDENCE",
        "agent_LAYAYA_MIN_CONFIDENCE",
    )
    laya_min_margin: float = _float(
        0.15,
        "AGENT_LAYAYA_MIN_MARGIN",
        "agent_LAYAYA_MIN_MARGIN",
    )
    # Jev (TypeSafe System One) reached through AIHubMix. The gateway exposes
    # the same /v1/systemone contract as the Laya sidecar, so both share one
    # client; only the endpoint, model, thresholds and retry policy differ.
    jev_base_url: str = _text("", "AGENT_JEV_BASE_URL", "agent_JEV_BASE_URL")
    jev_api_key: str = _text("", "AGENT_JEV_API_KEY", "agent_JEV_API_KEY")
    jev_model: str = _text("jev-1.13", "AGENT_JEV_MODEL", "agent_JEV_MODEL")
    jev_timeout_seconds: float = _float(
        10.0, "AGENT_JEV_TIMEOUT_SECONDS", "agent_JEV_TIMEOUT_SECONDS"
    )
    jev_max_retries: int = _int(
        2, "AGENT_JEV_MAX_RETRIES", "agent_JEV_MAX_RETRIES"
    )
    jev_min_confidence: float = _float(
        0.90, "AGENT_JEV_MIN_CONFIDENCE", "agent_JEV_MIN_CONFIDENCE"
    )
    jev_min_margin: float = _float(
        0.15, "AGENT_JEV_MIN_MARGIN", "agent_JEV_MIN_MARGIN"
    )
    understanding_min_confidence: float = _float(
        0.70,
        "AGENT_UNDERSTANDING_MIN_CONFIDENCE",
        "agent_UNDERSTANDING_MIN_CONFIDENCE",
    )
    understanding_min_confidence_collected: float = _float(
        0.85,
        "AGENT_UNDERSTANDING_MIN_CONFIDENCE_COLLECTED",
        "agent_UNDERSTANDING_MIN_CONFIDENCE_COLLECTED",
    )

    # Execution budgets from the step-1 specification.
    task_max_steps: int = _int(8, "AGENT_TASK_MAX_STEPS", "agent_TASK_MAX_STEPS")
    task_max_execution_seconds: float = _float(
        300.0, "AGENT_TASK_MAX_EXECUTION_SECONDS", "agent_TASK_MAX_EXECUTION_SECONDS"
    )
    task_max_retries: int = _int(3, "AGENT_TASK_MAX_RETRIES", "agent_TASK_MAX_RETRIES")
    task_max_model_calls: int = _int(
        8, "AGENT_TASK_MAX_MODEL_CALLS", "agent_TASK_MAX_MODEL_CALLS"
    )
    task_max_replans: int = _int(
        3, "AGENT_TASK_MAX_REPLANS", "agent_TASK_MAX_REPLANS"
    )
    # How many times in a row one capability may be retried before the kernel
    # treats it as a loop. A research Plan legitimately fetches several pages,
    # so the limit counts an unbroken run rather than every use.
    task_max_same_capability_calls: int = _int(
        4,
        "AGENT_TASK_MAX_SAME_CAPABILITY_CALLS",
        "agent_TASK_MAX_SAME_CAPABILITY_CALLS",
    )
    task_retry_backoff_seconds: str = _text(
        "1,2,4", "AGENT_TASK_RETRY_BACKOFF_SECONDS", "agent_TASK_RETRY_BACKOFF_SECONDS"
    )

    # Step 4 web capabilities: public pages only, read-only, size capped.
    web_timeout_seconds: float = _float(
        10.0, "AGENT_WEB_TIMEOUT_SECONDS", "agent_WEB_TIMEOUT_SECONDS"
    )
    web_max_bytes: int = _int(
        512 * 1024, "AGENT_WEB_MAX_BYTES", "agent_WEB_MAX_BYTES"
    )
    web_max_redirects: int = _int(
        3, "AGENT_WEB_MAX_REDIRECTS", "agent_WEB_MAX_REDIRECTS"
    )
    # Off by default: only switch it on where the resolver hands out non-public
    # addresses on purpose (fake-IP proxies), otherwise the fetch guard blocks
    # every request in that environment.
    web_allow_private_addresses: bool = _bool(
        False,
        "AGENT_WEB_ALLOW_PRIVATE_ADDRESSES",
        "agent_WEB_ALLOW_PRIVATE_ADDRESSES",
    )
    # Step 5 web research: one Planner-visible capability with two sidecars
    # behind it. Empty base URLs mean "that sidecar is not deployed here", which
    # degrades to link reading (search) or static-only reading (render).
    searxng_base_url: str = _text(
        "", "AGENT_SEARXNG_BASE_URL", "agent_SEARXNG_BASE_URL"
    )
    searxng_timeout_seconds: float = _float(
        10.0, "AGENT_SEARXNG_TIMEOUT_SECONDS", "agent_SEARXNG_TIMEOUT_SECONDS"
    )
    searxng_language: str = _text(
        "zh-CN", "AGENT_SEARXNG_LANGUAGE", "agent_SEARXNG_LANGUAGE"
    )
    crawl4ai_base_url: str = _text(
        "", "AGENT_CRAWL4AI_BASE_URL", "agent_CRAWL4AI_BASE_URL"
    )
    # The official Crawl4AI image requires this on every endpoint; without it
    # the sidecar answers only inside its own container.
    crawl4ai_api_token: str = _text(
        "", "AGENT_CRAWL4AI_API_TOKEN", "agent_CRAWL4AI_API_TOKEN"
    )
    crawl4ai_timeout_seconds: float = _float(
        30.0, "AGENT_CRAWL4AI_TIMEOUT_SECONDS", "agent_CRAWL4AI_TIMEOUT_SECONDS"
    )
    web_research_timeout_seconds: float = _float(
        90.0,
        "AGENT_WEB_RESEARCH_TIMEOUT_SECONDS",
        "agent_WEB_RESEARCH_TIMEOUT_SECONDS",
    )
    # Below this many characters a static body is treated as a stub and the
    # renderer gets a turn: JavaScript shells and anti-bot interstitials look
    # exactly like short pages to a plain GET.
    web_research_min_text_chars: int = _int(
        500,
        "AGENT_WEB_RESEARCH_MIN_TEXT_CHARS",
        "agent_WEB_RESEARCH_MIN_TEXT_CHARS",
    )
    web_research_max_results: int = _int(
        5, "AGENT_WEB_RESEARCH_MAX_RESULTS", "agent_WEB_RESEARCH_MAX_RESULTS"
    )
    web_research_max_pages: int = _int(
        3, "AGENT_WEB_RESEARCH_MAX_PAGES", "agent_WEB_RESEARCH_MAX_PAGES"
    )
    web_research_max_queries: int = _int(
        3, "AGENT_WEB_RESEARCH_MAX_QUERIES", "agent_WEB_RESEARCH_MAX_QUERIES"
    )
    # Per page and overall body budgets handed to answer.compose.
    web_research_page_chars: int = _int(
        8000, "AGENT_WEB_RESEARCH_PAGE_CHARS", "agent_WEB_RESEARCH_PAGE_CHARS"
    )
    web_research_max_evidence_chars: int = _int(
        60_000,
        "AGENT_WEB_RESEARCH_MAX_EVIDENCE_CHARS",
        "agent_WEB_RESEARCH_MAX_EVIDENCE_CHARS",
    )
    web_research_alias_path: str = _text(
        "",
        "AGENT_WEB_RESEARCH_ALIAS_PATH",
        "agent_WEB_RESEARCH_ALIAS_PATH",
    )
    # Which search provider and which rendering fallback are in use. Both layers
    # have a self-hosted and a hosted implementation, so switching vendors is a
    # setting rather than a code change.
    web_search_provider: str = _text(
        "searxng", "AGENT_WEB_SEARCH_PROVIDER", "agent_WEB_SEARCH_PROVIDER"
    )
    # Empty means "auto": use Crawl4AI when its base URL is set, else nothing.
    web_renderer: str = _text(
        "", "AGENT_WEB_RENDERER", "agent_WEB_RENDERER"
    )
    tavily_api_key: str = _text(
        "", "AGENT_TAVILY_API_KEY", "agent_TAVILY_API_KEY"
    )
    tavily_base_url: str = _text(
        "https://api.tavily.com", "AGENT_TAVILY_BASE_URL", "agent_TAVILY_BASE_URL"
    )
    tavily_search_depth: str = _text(
        "basic", "AGENT_TAVILY_SEARCH_DEPTH", "agent_TAVILY_SEARCH_DEPTH"
    )
    tavily_extract_depth: str = _text(
        "basic", "AGENT_TAVILY_EXTRACT_DEPTH", "agent_TAVILY_EXTRACT_DEPTH"
    )
    tavily_include_raw_content: bool = _bool(
        False,
        "AGENT_TAVILY_INCLUDE_RAW_CONTENT",
        "agent_TAVILY_INCLUDE_RAW_CONTENT",
    )
    tavily_timeout_seconds: float = _float(
        30.0, "AGENT_TAVILY_TIMEOUT_SECONDS", "agent_TAVILY_TIMEOUT_SECONDS"
    )
    answer_timeout_seconds: float = _float(
        60.0, "AGENT_ANSWER_TIMEOUT_SECONDS", "agent_ANSWER_TIMEOUT_SECONDS"
    )
    answer_max_output_tokens: int = _int(
        1200, "AGENT_ANSWER_MAX_OUTPUT_TOKENS", "agent_ANSWER_MAX_OUTPUT_TOKENS"
    )
    # Chat replies cost one model call per non-task message. The switch exists
    # so an operator can stop paying for greetings without a code change; when
    # it is off the capability is not registered and the planner simply has
    # nothing to call, which is the pre-reply behaviour.
    chat_reply_enabled: bool = _bool(
        True, "AGENT_CHAT_REPLY_ENABLED", "agent_CHAT_REPLY_ENABLED"
    )
    chat_reply_timeout_seconds: float = _float(
        30.0, "AGENT_CHAT_REPLY_TIMEOUT_SECONDS", "agent_CHAT_REPLY_TIMEOUT_SECONDS"
    )
    chat_reply_max_output_tokens: int = _int(
        300, "AGENT_CHAT_REPLY_MAX_OUTPUT_TOKENS", "agent_CHAT_REPLY_MAX_OUTPUT_TOKENS"
    )
    approval_expires_seconds: float = _float(
        3600.0, "AGENT_APPROVAL_EXPIRES_SECONDS", "agent_APPROVAL_EXPIRES_SECONDS"
    )
    task_lease_seconds: float = _float(
        120.0, "AGENT_TASK_LEASE_SECONDS", "agent_TASK_LEASE_SECONDS"
    )

    worker_concurrency: int = _int(
        4, "AGENT_WORKER_CONCURRENCY", "agent_WORKER_CONCURRENCY"
    )
    outbox_batch_size: int = _int(
        50, "AGENT_OUTBOX_BATCH_SIZE", "agent_OUTBOX_BATCH_SIZE"
    )

    # Multimodal attachment support (temporary storage)
    attachment_minio_bucket: str = _text(
        "agent-temp",
        "AGENT_ATTACHMENT_MINIO_BUCKET",
        "agent_ATTACHMENT_MINIO_BUCKET"
    )
    attachment_ttl_hours: int = _int(
        24,
        "AGENT_ATTACHMENT_TTL_HOURS",
        "agent_ATTACHMENT_TTL_HOURS"
    )
    attachment_max_size_bytes: int = _int(
        20_971_520,  # 20MB
        "AGENT_ATTACHMENT_MAX_SIZE_BYTES",
        "agent_ATTACHMENT_MAX_SIZE_BYTES"
    )

    # MinIO configuration
    minio_endpoint: str = _text(
        "minio:9000",
        "AGENT_MINIO_ENDPOINT",
        "agent_MINIO_ENDPOINT"
    )
    minio_access_key: str = _text(
        "minioadmin",
        "AGENT_MINIO_ACCESS_KEY",
        "agent_MINIO_ACCESS_KEY"
    )
    minio_secret_key: str = _text(
        "minioadmin",
        "AGENT_MINIO_SECRET_KEY",
        "agent_MINIO_SECRET_KEY"
    )

    # RAG service configuration (for internal attachment parsing)
    rag_service_url: str = _text(
        "http://rag-service:8000",
        "AGENT_RAG_SERVICE_URL",
        "agent_RAG_SERVICE_URL"
    )
    rag_internal_token: str = _text(
        "local-development-only",
        "AGENT_RAG_INTERNAL_TOKEN",
        "agent_RAG_INTERNAL_TOKEN"
    )

    @property
    def retry_backoff_seconds(self) -> list[float]:
        values: list[float] = []
        for item in self.task_retry_backoff_seconds.split(","):
            item = item.strip()
            if not item:
                continue
            values.append(float(item))
        return values or [1.0]

    @property
    def knowledge_platform_allowlist(self) -> tuple[str, ...]:
        """Platforms whose collected messages may become Agent Tasks."""

        values = [item.strip().lower() for item in self.knowledge_platforms.split(",")]
        return tuple(item for item in values if item) or DEFAULT_KNOWLEDGE_PLATFORMS


settings = Settings()
