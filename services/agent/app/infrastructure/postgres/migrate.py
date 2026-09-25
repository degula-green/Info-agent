"""Explicit migration runner for the Agent schema.

The application and the worker never execute DDL. Run this module (or the
``agent-migrate`` container) to apply or roll back the Agent runtime schema.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = SERVICE_ROOT.parents[1]

load_dotenv(SERVICE_ROOT / ".env", override=False)

from app.config import settings  # noqa: E402

DEFAULT_MIGRATION = REPO_ROOT / "db" / "migrations" / "20260925_agent_runtime_rebuild.sql"
DEFAULT_ROLLBACK = REPO_ROOT / "db" / "migrations" / "20260925_agent_runtime_rebuild.down.sql"


def apply_sql(path: Path) -> None:
    if not settings.database_url:
        raise RuntimeError("AGENT_DATABASE_URL is not configured")
    if not path.exists():
        raise FileNotFoundError(f"migration file not found: {path}")
    import psycopg

    sql = path.read_text(encoding="utf-8")
    with psycopg.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the Agent schema migration")
    parser.add_argument("--file", type=Path, default=None, help="SQL file to execute")
    parser.add_argument("--rollback", action="store_true", help="run the down migration instead")
    args = parser.parse_args(argv)

    target = args.file or (DEFAULT_ROLLBACK if args.rollback else DEFAULT_MIGRATION)
    try:
        apply_sql(target)
    except Exception as exc:  # pragma: no cover - operational tooling
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"migration applied: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
