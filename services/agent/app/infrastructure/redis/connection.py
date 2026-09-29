"""Redis connection built from the Agent settings."""

from __future__ import annotations

from typing import Any

from app.config import Settings


def build_redis(settings: Settings) -> Any:
    if not settings.redis_url:
        raise RuntimeError("AGENT_REDIS_URL is not configured")
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - redis is a hard dependency
        raise RuntimeError("redis package is required for Streams") from exc

    url = settings.redis_url
    if settings.redis_tls and url.startswith("redis://"):
        url = "rediss://" + url[len("redis://") :]
    read_timeout = max(0.1, settings.redis_block_ms / 1000 + 1)
    return redis.Redis.from_url(
        url,
        db=settings.redis_database,
        username=settings.redis_username or None,
        password=settings.redis_password or None,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=read_timeout,
    )
