"""Tree quality metrics that need no labelled data.

These come first on purpose: they answer "is the tree doing anything at all"
before any accuracy number is available. A mount coverage of zero makes every
other quality figure meaningless, and it is visible on day one.
"""

from __future__ import annotations

from typing import Any


# Thresholds are guard rails, not targets: they mark the states that make the
# rest of the metrics uninterpretable.
CANDIDATE_BACKLOG_ALERT = 100
MAX_UNEMBEDDED_RATIO = 0.2
# Above this share of queries failing to resolve any entity, the tree is mostly
# adding a round trip without narrowing anything.
MAX_NO_ENTITY_MATCH_RATE = 0.5
# Above this share of windows coming back empty, the extraction model is costing
# calls without producing mounts. Measured baseline after the prompt fix was
# 18.75%, so 50% means something regressed.
MAX_EMPTY_WINDOW_RATIO = 0.5
SEARCH_WINDOW_HOURS = 24
SCAN_RUN_WINDOW = 50


class TreeMetricsService:
    def __init__(self, *, repository: Any) -> None:
        self.repository = repository

    def snapshot(self, *, scope_type: str, scope_id: str) -> dict[str, Any]:
        metrics = self.repository.tree_metrics(scope_type=scope_type, scope_id=scope_id)
        rows = self.repository.list_search_diagnostics(
            scope_type=scope_type, scope_id=scope_id,
            window_hours=SEARCH_WINDOW_HOURS, limit=2000,
        )
        metrics["search"] = summarize_search(rows, window_hours=SEARCH_WINDOW_HOURS)
        metrics["scan"] = summarize_scan_runs(
            self.repository.list_scan_runs(
                scope_type=scope_type, scope_id=scope_id, limit=SCAN_RUN_WINDOW
            )
        )
        metrics["alerts"] = evaluate_alerts(metrics)
        return metrics


def summarize_scan_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate recent window-scan sweeps.

    The empty-window ratio is the number worth watching: it separates "this
    conversation had nothing to extract" from "the model skipped the window",
    and only the ratio over many sweeps can tell the two apart.
    """
    if not runs:
        return {
            "run_count": 0, "conversations": 0, "windows": 0,
            "empty_windows": 0, "empty_window_ratio": 0.0,
            "mounts": 0, "candidates": 0, "relations": 0,
            "failed_conversations": 0, "last_run_at": None,
        }
    windows = sum(int(run.get("windows") or 0) for run in runs)
    empty = sum(int(run.get("empty_windows") or 0) for run in runs)
    return {
        "run_count": len(runs),
        "conversations": sum(int(run.get("conversations") or 0) for run in runs),
        "windows": windows,
        "empty_windows": empty,
        "empty_window_ratio": round(empty / windows, 4) if windows else 0.0,
        "mounts": sum(int(run.get("mounts") or 0) for run in runs),
        "candidates": sum(int(run.get("candidates") or 0) for run in runs),
        "relations": sum(int(run.get("relations") or 0) for run in runs),
        "failed_conversations": sum(
            int(run.get("failed_conversations") or 0) for run in runs
        ),
        "last_run_at": runs[0].get("created_at"),
    }


def summarize_search(
    rows: list[dict[str, Any]], *, window_hours: int = SEARCH_WINDOW_HOURS
) -> dict[str, Any]:
    """Aggregate retrieval diagnostics into the rates the plan asks to watch.

    This is what makes "降级率可查" true: the fallback and degradation reasons
    were already recorded per query, but nothing turned them into a number.
    """
    total = len(rows)
    paths: dict[str, int] = {}
    fallbacks: dict[str, int] = {}
    degraded: dict[str, int] = {}
    durations: list[int] = []
    llm_invoked = 0
    resolved = 0
    for row in rows:
        path = str(row.get("execution_path") or "unknown")
        paths[path] = paths.get(path, 0) + 1
        reason = str(row.get("fallback_reason") or "").strip()
        if reason:
            fallbacks[reason] = fallbacks.get(reason, 0) + 1
        for item in str(row.get("degraded_reason") or "").split(","):
            value = item.strip()
            if value:
                degraded[value] = degraded.get(value, 0) + 1
        durations.append(int(row.get("duration_ms") or 0))
        if row.get("llm_invoked"):
            llm_invoked += 1
        if int(row.get("resolved_entity_count") or 0) > 0:
            resolved += 1
    durations.sort()

    def percentile(fraction: float) -> int:
        if not durations:
            return 0
        index = min(len(durations) - 1, int(len(durations) * fraction))
        return durations[index]

    return {
        "window_hours": int(window_hours),
        "query_count": total,
        "execution_paths": paths,
        "fallback_reasons": fallbacks,
        "degraded_reasons": degraded,
        "resolved_entity_rate": round(resolved / total, 4) if total else 0.0,
        "no_entity_match_rate": (
            round(fallbacks.get("no_entity_match", 0) / total, 4) if total else 0.0
        ),
        "l4_invocation_rate": round(llm_invoked / total, 4) if total else 0.0,
        "latency_ms": {
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "max": durations[-1] if durations else 0,
        },
    }


def evaluate_alerts(metrics: dict[str, Any]) -> list[str]:
    alerts: list[str] = []
    message_count = int(metrics.get("message_count") or 0)
    entity_count = int(metrics.get("entity_count") or 0)
    mount_count = int(metrics.get("mount_count") or 0)

    if message_count and entity_count and not mount_count:
        # The registry has entities but nothing is reachable from them: either
        # the scan never ran or every mount attempt failed.
        alerts.append("no_mounts")
    if int(metrics.get("pending_candidate_count") or 0) > CANDIDATE_BACKLOG_ALERT:
        alerts.append("candidate_backlog")
    if entity_count:
        missing = int(metrics.get("entities_missing_embedding") or 0)
        if missing / entity_count > MAX_UNEMBEDDED_RATIO:
            # L3 cannot resolve an entity it has no vector for.
            alerts.append("entities_missing_embedding")
    if message_count and entity_count and not int(metrics.get("relation_count") or 0):
        alerts.append("no_relations")
    search = metrics.get("search") or {}
    query_count = int(search.get("query_count") or 0)
    if query_count and float(search.get("no_entity_match_rate") or 0) > MAX_NO_ENTITY_MATCH_RATE:
        # Most queries cannot resolve an entity, so the tree is mostly adding a
        # round trip without narrowing anything.
        alerts.append("high_no_entity_match")
    scan = metrics.get("scan") or {}
    if int(scan.get("windows") or 0) and (
        float(scan.get("empty_window_ratio") or 0) > MAX_EMPTY_WINDOW_RATIO
    ):
        # The extraction model is burning calls on windows it returns nothing
        # for; mount coverage will lag behind the scan frequency.
        alerts.append("high_empty_window_rate")
    return alerts
