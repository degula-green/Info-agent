from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.models import SearchRequest


@dataclass(frozen=True)
class QueryPlan:
    normalized_query: str
    use_vector: bool
    prefer_keyword: bool
    terms: tuple[str, ...]


_EXACT_HINTS = re.compile(r"(?:[A-Z]{2,}-?\d+|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\b\d+(?:\.\d+)?\b)")


def plan_query(request: SearchRequest) -> QueryPlan:
    normalized = " ".join(request.query.split())
    if not normalized:
        return QueryPlan("", False, True, ())
    exact = bool(_EXACT_HINTS.search(normalized)) or len(normalized) <= 3
    semantic_words = any(word in normalized for word in ("为什么", "原因", "如何", "方案", "相关", "区别", "解释", "how", "why"))
    use_vector = request.entry in {"global", "ai"} and not (exact and not semantic_words)
    return QueryPlan(normalized, use_vector, exact, tuple(normalized.split()))
