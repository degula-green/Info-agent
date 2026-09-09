from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from app.domain.models import SearchResult


def reciprocal_rank_fusion(
    lists: dict[str, list[SearchResult]],
    *,
    k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[SearchResult]:
    scores: dict[str, float] = defaultdict(float)
    values: dict[str, SearchResult] = {}
    weights = weights or {}
    for name, results in lists.items():
        weight = float(weights.get(name, 1.0))
        for rank, result in enumerate(results, start=1):
            key = result.chunk_id or f"{result.knowledge_item_id}:{result.attachment_id}:{rank}"
            scores[key] += weight / (k + rank)
            if key not in values or result.score > values[key].score:
                values[key] = result
    ordered = sorted(values.items(), key=lambda item: (-scores[item[0]], item[0]))
    output: list[SearchResult] = []
    for rank, (key, result) in enumerate(ordered, start=1):
        result.score = scores[key]
        result.rank = rank
        output.append(result)
    return output


def deduplicate_results(results: Iterable[SearchResult], *, max_per_item: int, limit: int) -> list[SearchResult]:
    counts: dict[str, int] = defaultdict(int)
    output: list[SearchResult] = []
    for result in results:
        group = result.attachment_id or result.knowledge_item_id or result.chunk_id
        if counts[group] >= max_per_item:
            continue
        counts[group] += 1
        result.rank = len(output) + 1
        output.append(result)
        if len(output) >= limit:
            break
    return output
