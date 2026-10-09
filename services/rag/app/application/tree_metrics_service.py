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
# The retrieval rates only describe requests that actually ran entity location.
#
# This used to be a blacklist of non-retrieval paths (`scope_export`), which was
# the wrong shape twice over: it needs every non-retrieval path to be known in
# advance, and one build already writes those rows with a NULL path. Since the
# entry split the label alone is not enough either - `traditional` now covers
# both "located, then fell back" and "hybrid entry that never located at all".
# So the gate is the thing being measured: did location run?
RETRIEVAL_PATHS = frozenset({"tree", "tree_shadow", "traditional", "metadata_filter"})
# A scrape re-runs the snapshot per scope, and each snapshot is several queries.
# Keeping the fan-out small bounds scrape cost; the per-scope detail is still
# available from the authorised admin endpoint.
DEFAULT_SCOPE_LIMIT = 20

PROMETHEUS_PREFIX = "rag_tree"

_METRIC_HELP = {
    "entity_count": "Active entities in the scope",
    "message_count": "Active message chunks in the scope",
    "mounted_chunk_count": "Message chunks reachable from at least one entity",
    "mount_count": "Active chunk-to-entity mounts",
    "mount_coverage": "mounted_chunk_count / message_count",
    "pending_candidate_count": "Candidates waiting for review",
    "relation_count": "Entity-to-entity relations",
    "review_count": "Candidate review decisions recorded in the scope",
    "review_approval_rate": "Share of decided reviews that accepted the candidate",
    "review_duration_avg_seconds": "Average reported review dwell time, in seconds",
    "review_duration_sample_count": "Reviews that reported a dwell time",
    "entities_missing_embedding": "Active entities without a usable vector",
    "alert_count": "Number of quality guard-rail alerts currently firing",
    "search_query_count": "Retrievals in the metric window",
    "search_retrieval_query_count": "Metric-window rows that actually located entities",
    "search_shadow_skipped_count": "Shadow requests skipped by the sample rate",
    "search_no_entity_match_rate": "Share of retrievals that resolved no entity",
    "search_resolved_entity_rate": "Share of retrievals that resolved an entity",
    "search_l4_invocation_rate": "Share of retrievals that escalated to L4",
    "search_latency_p50_ms": "Retrieval latency p50 in milliseconds",
    "search_latency_p95_ms": "Retrieval latency p95 in milliseconds",
    "search_locate_p50_ms": "Entity location latency p50 in milliseconds (L4 excluded)",
    "search_locate_p95_ms": "Entity location latency p95 in milliseconds (L4 excluded)",
    "search_locate_sample_count": "Retrievals that ran location without escalating to L4",
    "search_locate_escalated_p95_ms": "Entity location latency p95 for L4-escalated retrievals",
    "scan_window_count": "Windows processed by recent scans",
    "scan_empty_window_ratio": "Share of scanned windows the model returned empty for",
    "scan_mount_count": "Mounts written by recent scans",
    "scan_candidate_count": "Candidates created by recent scans",
}


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

    def prometheus(self, *, scope_limit: int = DEFAULT_SCOPE_LIMIT) -> str:
        """Per-scope snapshots rendered for a Prometheus scrape."""
        samples: list[tuple[str, dict[str, Any]]] = []
        for scope in self.repository.list_active_scopes(limit=scope_limit):
            scope_key = f'{scope["scope_type"]}:{scope["scope_id"]}'
            samples.append((
                scope_key,
                self.snapshot(scope_type=scope["scope_type"], scope_id=scope["scope_id"]),
            ))
        return render_prometheus(samples)


def flatten_metrics(snapshot: dict[str, Any]) -> dict[str, float]:
    """Pull the dashboard-worthy numbers out of one scope's snapshot."""
    search = snapshot.get("search") or {}
    scan = snapshot.get("scan") or {}
    latency = search.get("latency_ms") or {}
    locate = search.get("locate_latency_ms") or {}
    escalated = search.get("locate_escalated_ms") or {}
    return {
        f"{PROMETHEUS_PREFIX}_entity_count": snapshot.get("entity_count") or 0,
        f"{PROMETHEUS_PREFIX}_message_count": snapshot.get("message_count") or 0,
        f"{PROMETHEUS_PREFIX}_mounted_chunk_count": snapshot.get("mounted_chunk_count") or 0,
        f"{PROMETHEUS_PREFIX}_mount_count": snapshot.get("mount_count") or 0,
        f"{PROMETHEUS_PREFIX}_mount_coverage": snapshot.get("mount_coverage") or 0.0,
        f"{PROMETHEUS_PREFIX}_pending_candidate_count": snapshot.get("pending_candidate_count") or 0,
        f"{PROMETHEUS_PREFIX}_relation_count": snapshot.get("relation_count") or 0,
        f"{PROMETHEUS_PREFIX}_review_count": snapshot.get("review_count") or 0,
        f"{PROMETHEUS_PREFIX}_review_approval_rate": (
            snapshot.get("review_approval_rate") or 0.0
        ),
        # Prometheus convention is base units, so the stored milliseconds are
        # exposed as seconds.
        f"{PROMETHEUS_PREFIX}_review_duration_avg_seconds": round(
            float(snapshot.get("review_duration_avg_ms") or 0.0) / 1000.0, 3
        ),
        f"{PROMETHEUS_PREFIX}_review_duration_sample_count": (
            snapshot.get("review_duration_sample_count") or 0
        ),
        f"{PROMETHEUS_PREFIX}_entities_missing_embedding": (
            snapshot.get("entities_missing_embedding") or 0
        ),
        f"{PROMETHEUS_PREFIX}_alert_count": len(snapshot.get("alerts") or []),
        f"{PROMETHEUS_PREFIX}_search_query_count": search.get("query_count") or 0,
        f"{PROMETHEUS_PREFIX}_search_retrieval_query_count": (
            search.get("retrieval_query_count") or 0
        ),
        f"{PROMETHEUS_PREFIX}_search_shadow_skipped_count": (
            search.get("shadow_skipped_count") or 0
        ),
        f"{PROMETHEUS_PREFIX}_search_no_entity_match_rate": (
            search.get("no_entity_match_rate") or 0.0
        ),
        f"{PROMETHEUS_PREFIX}_search_resolved_entity_rate": (
            search.get("resolved_entity_rate") or 0.0
        ),
        f"{PROMETHEUS_PREFIX}_search_l4_invocation_rate": (
            search.get("l4_invocation_rate") or 0.0
        ),
        f"{PROMETHEUS_PREFIX}_search_latency_p50_ms": latency.get("p50") or 0,
        f"{PROMETHEUS_PREFIX}_search_latency_p95_ms": latency.get("p95") or 0,
        f"{PROMETHEUS_PREFIX}_search_locate_p50_ms": locate.get("p50") or 0,
        f"{PROMETHEUS_PREFIX}_search_locate_p95_ms": locate.get("p95") or 0,
        f"{PROMETHEUS_PREFIX}_search_locate_sample_count": (
            locate.get("sample_count") or 0
        ),
        f"{PROMETHEUS_PREFIX}_search_locate_escalated_p95_ms": (
            escalated.get("p95") or 0
        ),
        f"{PROMETHEUS_PREFIX}_scan_window_count": scan.get("windows") or 0,
        f"{PROMETHEUS_PREFIX}_scan_empty_window_ratio": (
            scan.get("empty_window_ratio") or 0.0
        ),
        f"{PROMETHEUS_PREFIX}_scan_mount_count": scan.get("mounts") or 0,
        f"{PROMETHEUS_PREFIX}_scan_candidate_count": scan.get("candidates") or 0,
    }


def render_prometheus(samples: list[tuple[str, dict[str, Any]]]) -> str:
    """Prometheus text exposition, one series per scope.

    Everything here is a gauge: counts, ratios and latency percentiles are all
    point-in-time values, and the scope label keeps a single dashboard usable
    for every tenant.
    """
    series: dict[str, dict[str, float]] = {}
    for scope_key, snapshot in samples:
        for name, value in flatten_metrics(snapshot).items():
            series.setdefault(name, {})[scope_key] = float(value)
    lines: list[str] = []
    for name in sorted(series):
        short = name[len(PROMETHEUS_PREFIX) + 1:]
        lines.append(f"# HELP {name} {_METRIC_HELP.get(short, short)}")
        lines.append(f"# TYPE {name} gauge")
        for scope_key in sorted(series[name]):
            lines.append(f'{name}{{scope="{scope_key}"}} {series[name][scope_key]}')
    return "\n".join(lines) + "\n"


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
    attempts = [
        row
        for row in rows
        if str(row.get("execution_path") or "") in RETRIEVAL_PATHS
        # Location ran. `locate_ms` is 0 both for bulk exports and for the
        # hybrid entries that skip the locator entirely, and neither belongs in
        # a rate about location quality.
        and float(row.get("locate_ms") or 0) > 0
        # A shadow request skipped by sampling never ran location, so its empty
        # result is a budgeting decision, not a location miss.
        and row.get("shadow_sampled", True) is not False
    ]
    shadow_skipped = sum(1 for row in rows if row.get("shadow_sampled") is False)
    paths: dict[str, int] = {}
    fallbacks: dict[str, int] = {}
    degraded: dict[str, int] = {}
    durations: list[int] = []
    locate_durations: list[int] = []
    escalated_durations: list[int] = []
    llm_invoked = 0
    resolved = 0
    unmatched = 0
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
    for row in attempts:
        durations.append(int(row.get("duration_ms") or 0))
        invoked = bool(row.get("llm_invoked"))
        # The plan keeps the escalated (L4) path out of the normal percentile:
        # one LLM round trip would otherwise define the "常规路径" number.
        locate_ms = int(float(row.get("locate_ms") or 0.0))
        if locate_ms > 0:
            (escalated_durations if invoked else locate_durations).append(locate_ms)
        if invoked:
            llm_invoked += 1
        if int(row.get("resolved_entity_count") or 0) > 0:
            resolved += 1
        if str(row.get("fallback_reason") or "").strip() == "no_entity_match":
            unmatched += 1
    durations.sort()
    locate_durations.sort()
    escalated_durations.sort()
    attempted = len(attempts)

    def percentile(values: list[int], fraction: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, int(len(values) * fraction))
        return values[index]

    def distribution(values: list[int]) -> dict[str, int]:
        return {
            "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95),
            "max": values[-1] if values else 0,
            "sample_count": len(values),
        }

    return {
        "window_hours": int(window_hours),
        "query_count": total,
        "retrieval_query_count": attempted,
        "shadow_skipped_count": shadow_skipped,
        "execution_paths": paths,
        "fallback_reasons": fallbacks,
        "degraded_reasons": degraded,
        "resolved_entity_rate": round(resolved / attempted, 4) if attempted else 0.0,
        "no_entity_match_rate": round(unmatched / attempted, 4) if attempted else 0.0,
        "l4_invocation_rate": round(llm_invoked / attempted, 4) if attempted else 0.0,
        "latency_ms": distribution(durations),
        "locate_latency_ms": distribution(locate_durations),
        "locate_escalated_ms": distribution(escalated_durations),
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
    # Gate on retrieval attempts: a window holding nothing but scope exports has
    # no location work to judge, and its 0% rate must not read as "healthy".
    attempts = int(search.get("retrieval_query_count", search.get("query_count") or 0) or 0)
    if attempts and float(search.get("no_entity_match_rate") or 0) > MAX_NO_ENTITY_MATCH_RATE:
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
