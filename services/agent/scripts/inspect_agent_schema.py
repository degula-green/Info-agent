"""Read-only preflight inspection of the PostgreSQL ``agent`` schema.

Run before executing the destructive rebuild migration so the existing old
tables, their columns, foreign keys, indexes and row counts are known.
Connection settings come from ``services/agent/.env`` and process environment
variables. Credentials are never printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

load_dotenv(SERVICE_ROOT / ".env", override=False)

from app.config import settings  # noqa: E402


def main() -> int:
    if not settings.database_url:
        print("AGENT_DATABASE_URL is not configured")
        return 2
    try:
        import psycopg
    except ImportError:
        print("psycopg is required: run uv sync inside services/agent")
        return 2

    schema = settings.database_schema
    try:
        with psycopg.connect(settings.database_url, connect_timeout=8) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select table_name from information_schema.tables "
                    "where table_schema = %s and table_type = 'BASE TABLE' order by table_name",
                    (schema,),
                )
                tables = [row[0] for row in cursor.fetchall()]
                print(f"schema: {schema}")
                print(f"table_count: {len(tables)}")
                for table in tables:
                    cursor.execute(f'select count(*) from "{schema}"."{table}"')
                    row_count = cursor.fetchone()[0]
                    cursor.execute(
                        "select column_name, data_type, is_nullable from information_schema.columns "
                        "where table_schema = %s and table_name = %s order by ordinal_position",
                        (schema, table),
                    )
                    columns = cursor.fetchall()
                    cursor.execute(
                        "select tc.constraint_type, kcu.column_name, ccu.table_name, ccu.column_name "
                        "from information_schema.table_constraints tc "
                        "left join information_schema.key_column_usage kcu "
                        "  on kcu.constraint_name = tc.constraint_name and kcu.table_schema = tc.table_schema "
                        "left join information_schema.constraint_column_usage ccu "
                        "  on ccu.constraint_name = tc.constraint_name and ccu.table_schema = tc.table_schema "
                        f'where tc.table_schema = %s and tc.table_name = %s',
                        (schema, table),
                    )
                    constraints = cursor.fetchall()
                    print(f"- {table}: rows={row_count}")
                    for name, data_type, nullable in columns:
                        print(f"    column {name} {data_type} nullable={nullable}")
                    for constraint_type, column, ref_table, ref_column in constraints:
                        reference = f" -> {ref_table}.{ref_column}" if ref_table else ""
                        print(f"    {constraint_type} {column}{reference}")
    except Exception as exc:  # pragma: no cover - operational tooling
        print(f"inspection failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
