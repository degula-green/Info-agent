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

BOOTSTRAP_MIGRATIONS = [
    REPO_ROOT / "db" / "migrations" / "20260925_agent_runtime_rebuild.sql",
    REPO_ROOT / "db" / "migrations" / "20260927_agent_dynamic_plan.sql",
    REPO_ROOT / "db" / "migrations" / "20260927_agent_approval_binding.sql",
    REPO_ROOT / "db" / "migrations" / "20260927_agent_todo_ledger.sql",
    REPO_ROOT / "db" / "migrations" / "20261002_agent_conversation_history.up.sql",
    REPO_ROOT / "db" / "migrations" / "20261003_agent_conversation_memory_phase1.up.sql",
    REPO_ROOT / "db" / "migrations" / "20261003_agent_conversation_memory_phase2.up.sql",
    REPO_ROOT / "db" / "migrations" / "20261004_agent_plan_step_dependencies.up.sql",
    REPO_ROOT / "db" / "migrations" / "20261008_agent_person_fact_snapshots.up.sql",
]
BOOTSTRAP_ROLLBACKS = [
    REPO_ROOT / "db" / "migrations" / "20261008_agent_person_fact_snapshots.down.sql",
    REPO_ROOT / "db" / "migrations" / "20261004_agent_plan_step_dependencies.down.sql",
    REPO_ROOT / "db" / "migrations" / "20261003_agent_conversation_memory_phase2.down.sql",
    REPO_ROOT / "db" / "migrations" / "20261003_agent_conversation_memory_phase1.down.sql",
    REPO_ROOT / "db" / "migrations" / "20261002_agent_conversation_history.down.sql",
    REPO_ROOT / "db" / "migrations" / "20260927_agent_todo_ledger.down.sql",
    REPO_ROOT / "db" / "migrations" / "20260927_agent_approval_binding.down.sql",
    REPO_ROOT / "db" / "migrations" / "20260927_agent_dynamic_plan.down.sql",
    REPO_ROOT / "db" / "migrations" / "20260925_agent_runtime_rebuild.down.sql",
]
DEFAULT_MIGRATION = (
    REPO_ROOT / "db" / "migrations" / "20261004_agent_plan_step_dependencies.up.sql"
)
DEFAULT_ROLLBACK = (
    REPO_ROOT / "db" / "migrations" / "20261004_agent_plan_step_dependencies.down.sql"
)


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
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="create the complete Agent schema from an empty database",
    )
    args = parser.parse_args(argv)

    try:
        targets = (
            [args.file]
            if args.file is not None
            else (
                BOOTSTRAP_ROLLBACKS
                if args.bootstrap and args.rollback
                else BOOTSTRAP_MIGRATIONS
                if args.bootstrap
                else [DEFAULT_ROLLBACK if args.rollback else DEFAULT_MIGRATION]
            )
        )
        for target in targets:
            apply_sql(target)
            print(f"migration applied: {target}")
    except Exception as exc:  # pragma: no cover - operational tooling
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
