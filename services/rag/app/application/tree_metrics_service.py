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


class TreeMetricsService:
    def __init__(self, *, repository: Any) -> None:
        self.repository = repository

    def snapshot(self, *, scope_type: str, scope_id: str) -> dict[str, Any]:
        metrics = self.repository.tree_metrics(scope_type=scope_type, scope_id=scope_id)
        metrics["alerts"] = evaluate_alerts(metrics)
        return metrics


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
    return alerts
