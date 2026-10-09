"""Measure entity-location latency per layer on real queries.

    python scripts/measure_locate_latency.py \
        --scope-type user --scope-id <uuid> --limit 20
    python scripts/measure_locate_latency.py \
        --scope-type organization --scope-id <uuid> \
        --queries-file queries.txt --repeat 3 --json out.json

Queries come from ``--queries-file`` when given, otherwise from the scope's most
recent redacted search queries. Location runs in-process through the same
``EntityLocator`` the service uses, and nothing is written back.

This is the measurement behind the plan's "常规路径定位 p95 <= 80ms" gate
(Phase 1 decision point). L4 escalations are reported on their own line because
the plan keeps them out of the normal percentile.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.application.entity_locator import EntityLocator  # noqa: E402
from app.domain.location import LocateRequest  # noqa: E402
from app.infrastructure.embedding.client import EmbeddingClient  # noqa: E402
from app.infrastructure.persistence.mvp import PostgresRagMVPRepository  # noqa: E402

GATE_P95_MS = 80.0


def recent_queries(
    repository: PostgresRagMVPRepository,
    *,
    scope_type: str,
    scope_id: str,
    limit: int,
) -> list[str]:
    """Redacted query text for the scope, newest first.

    ``redact_query`` only masks long digit runs and e-mail addresses, so what
    comes back is still a usable query set rather than a placeholder.
    """
    with repository._connection() as connection:  # noqa: SLF001 - metric probe
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT query_redacted
                      FROM {repository.schema}.search_history
                     WHERE scope_type=%s AND scope_id=%s::uuid
                       AND COALESCE(query_redacted,'') <> ''
                     ORDER BY created_at DESC
                     LIMIT %s""",
                (scope_type, scope_id, max(1, int(limit))),
            )
            return [
                str(row[0]).strip()
                for row in cursor.fetchall()
                if str(row[0] or "").strip()
            ]


def percentile(values: list[float], fraction: float) -> float:
    """Same nearest-rank convention as TreeMetricsService, so numbers compare."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def summarize(runs: list[dict], layer_names: list[str]) -> dict:
    normal = [run for run in runs if not run["escalated"]]
    escalated = [run for run in runs if run["escalated"]]
    rows = {}
    for layer in layer_names + ["total"]:
        values = [
            float(run["total"] if layer == "total" else run["layers"].get(layer) or 0.0)
            for run in normal
        ]
        rows[layer] = {
            "p50": round(percentile(values, 0.5), 1),
            "p95": round(percentile(values, 0.95), 1),
            "max": round(max(values), 1) if values else 0.0,
        }
    escalated_values = [float(run["total"]) for run in escalated]
    return {
        "run_count": len(runs),
        "normal_count": len(normal),
        "escalated_count": len(escalated),
        "layers": rows,
        "escalated_total_p95": round(percentile(escalated_values, 0.95), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-type", default="user")
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--queries-file", type=Path)
    parser.add_argument("--limit", type=int, default=20, help="queries read from history")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", type=Path, help="write the summary here")
    args = parser.parse_args()

    if args.queries_file:
        queries = [
            line.strip()
            for line in args.queries_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        queries = recent_queries(
            PostgresRagMVPRepository(),
            scope_type=args.scope_type,
            scope_id=args.scope_id,
            limit=args.limit,
        )
    if not queries:
        print("没有可用的查询：换个 scope，或用 --queries-file 指定。")
        return 1

    locator = EntityLocator(
        repository=PostgresRagMVPRepository(), embedding=EmbeddingClient()
    )
    runs: list[dict] = []
    layer_names: list[str] = []
    for query in queries:
        for _ in range(max(1, args.repeat)):
            result = locator.locate(
                LocateRequest(
                    query=query,
                    scope_type=args.scope_type,
                    scope_id=args.scope_id,
                    top_k=args.top_k,
                )
            )
            diagnostics = result.diagnostics
            layers = diagnostics.get("layer_ms") or {}
            for layer in layers:
                if layer not in layer_names:
                    layer_names.append(layer)
            runs.append({
                "query": query,
                "total": float(diagnostics.get("locate_ms") or 0.0),
                "layers": {name: float(value) for name, value in layers.items()},
                "escalated": any(
                    bool(trace.get("llm_invoked"))
                    for trace in diagnostics.get("mentions") or []
                ),
                "located": int(diagnostics.get("located_count") or 0),
            })

    summary = summarize(runs, sorted(layer_names))
    print(f"scope        {args.scope_type}:{args.scope_id}")
    print(f"queries      {len(queries)} x {max(1, args.repeat)} = {summary['run_count']} runs")
    print(f"escalated    {summary['escalated_count']} (L4)")
    print()
    print(f"{'layer':8}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}")
    for layer, values in summary["layers"].items():
        print(f"{layer:8}{values['p50']:>10}{values['p95']:>10}{values['max']:>10}")
    published = summary["layers"]["total"]
    verdict = "达标" if published["p95"] and published["p95"] <= GATE_P95_MS else "未达标"
    print()
    print(f"gate  常规路径定位 p95 <= {GATE_P95_MS:g}ms  ->  {verdict}"
          f"（p95 = {published['p95']}ms，样本 {summary['normal_count']}）")
    if args.json:
        args.json.write_text(
            json.dumps({"summary": summary, "runs": runs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"written {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
