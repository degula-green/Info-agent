"""Per-request tree_mode resolution - the rollout control from plan 3.5.

Two knobs, one purpose: widen the blast radius on purpose instead of flipping a
global switch.

``RAG_TREE_MODE`` is the deployment default. ``RAG_TREE_ROLLOUT_SCOPES`` names
the scopes that are promoted to ``tree`` while everyone else keeps the default,
which is how "1 user -> 3 users -> 10 users" runs without a redeploy.

``off`` is deliberately a **hard kill switch**: it wins over the whitelist. The
rollback plan says "tree_mode 切回 shadow，仍异常则切 off", and a whitelist that
outranked the kill switch would leave exactly the scopes you were trying to
protect still running tree mode.

Shadow mode runs location on every query, which is the expensive half
(``locate`` measured at ~1s against a remote registry and embedding provider).
``RAG_TREE_SHADOW_SAMPLE_RATE`` bounds how many shadow requests actually pay
that cost. The decision is hashed from scope + query, so the same query does not
flap between sampled and skipped while a user retries it.
"""

from __future__ import annotations

import hashlib

TREE_MODES = ("off", "shadow", "tree")


def scope_key(scope_type: str, scope_id: str) -> str:
    return f"{scope_type}:{scope_id}"


def parse_rollout_scopes(raw: str | None) -> frozenset[str]:
    """Parse ``user:<id>,organization:<id>`` into a set of scope keys.

    Blank entries and duplicates are dropped; an entry without a ``:`` is
    ignored rather than guessed at, because a typo that silently whitelists
    nothing looks exactly like a rollout that has not started.
    """
    entries = set()
    for chunk in str(raw or "").replace(";", ",").split(","):
        value = chunk.strip()
        if not value or ":" not in value:
            continue
        scope_type, _, scope_id = value.partition(":")
        scope_type = scope_type.strip().lower()
        scope_id = scope_id.strip()
        if scope_type and scope_id:
            entries.add(f"{scope_type}:{scope_id}")
    return frozenset(entries)


def resolve_tree_mode(
    *,
    default: str,
    scope_type: str,
    scope_id: str,
    rollout_scopes: frozenset[str] = frozenset(),
) -> str:
    """The mode this request should actually run in."""
    resolved = str(default or "off").strip().lower()
    if resolved not in TREE_MODES:
        resolved = "off"
    if resolved == "off":
        # Kill switch: never outranked.
        return "off"
    if resolved == "tree":
        return "tree"
    if scope_key(scope_type, scope_id) in rollout_scopes:
        return "tree"
    return "shadow"


def shadow_sampled(
    *, scope_type: str, scope_id: str, query: str, sample_rate: float
) -> bool:
    """Deterministic decision so one query keeps its answer while it is retried."""
    try:
        rate = float(sample_rate)
    except (TypeError, ValueError):
        rate = 1.0
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256(
        f"{scope_key(scope_type, scope_id)}\n{query}".encode("utf-8")
    ).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate
