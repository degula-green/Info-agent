"""Read-only connectivity check for the Agent PostgreSQL and Redis settings.

Prints only status information; credentials are never echoed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

load_dotenv(SERVICE_ROOT / ".env", override=False)

from app.config import settings  # noqa: E402


def check_postgres() -> bool:
    if not settings.database_url:
        print("postgres: not configured (AGENT_DATABASE_URL is empty)")
        return False
    try:
        import psycopg

        with psycopg.connect(settings.database_url, connect_timeout=8) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
                cursor.fetchone()
                cursor.execute(
                    "select count(*) from information_schema.tables where table_schema = %s",
                    (settings.database_schema,),
                )
                tables = cursor.fetchone()[0]
        print(f"postgres: ok (schema={settings.database_schema}, tables={tables})")
        return True
    except Exception as exc:  # pragma: no cover - operational tooling
        print(f"postgres: failed ({type(exc).__name__}: {str(exc)[:200]})")
        return False


def check_redis() -> bool:
    if not settings.redis_url:
        print("redis: not configured (AGENT_REDIS_URL is empty)")
        return False
    try:
        from app.infrastructure.redis.connection import build_redis

        client = build_redis(settings)
        pong = client.ping()
        print(f"redis: ok (ping={bool(pong)}, stream={settings.redis_inbound_stream})")
        return True
    except Exception as exc:  # pragma: no cover - operational tooling
        print(f"redis: failed ({type(exc).__name__}: {str(exc)[:200]})")
        return False


def main() -> int:
    postgres_ok = check_postgres()
    redis_ok = check_redis()
    return 0 if (postgres_ok and redis_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
