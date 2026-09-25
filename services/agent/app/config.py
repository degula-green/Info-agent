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

    # Knowledge owns conversation visibility, calendar authorization and the
    # platform tokens; the Agent only calls its internal API.
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

    # Execution budgets from the step-1 specification.
    task_max_steps: int = _int(8, "AGENT_TASK_MAX_STEPS", "agent_TASK_MAX_STEPS")
    task_max_execution_seconds: float = _float(
        300.0, "AGENT_TASK_MAX_EXECUTION_SECONDS", "agent_TASK_MAX_EXECUTION_SECONDS"
    )
    task_max_retries: int = _int(3, "AGENT_TASK_MAX_RETRIES", "agent_TASK_MAX_RETRIES")
    task_retry_backoff_seconds: str = _text(
        "1,5,20", "AGENT_TASK_RETRY_BACKOFF_SECONDS", "agent_TASK_RETRY_BACKOFF_SECONDS"
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
