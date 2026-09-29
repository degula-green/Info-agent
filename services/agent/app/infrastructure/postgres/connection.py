"""PostgreSQL connection pool built from the Agent settings."""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from app.config import Settings


def build_pool(settings: Settings) -> ConnectionPool:
    if not settings.database_url:
        raise RuntimeError("AGENT_DATABASE_URL is not configured")
    return ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.database_min_pool_size,
        max_size=settings.database_max_pool_size,
        timeout=settings.database_connect_timeout_seconds,
        kwargs={"options": f"-c statement_timeout={int(settings.database_command_timeout_seconds * 1000)}"},
        open=True,
    )
