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
    retrieval_mode: str = "chunk"


_EXACT_HINTS = re.compile(r"(?:[A-Z]{2,}-?\d+|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\b\d+(?:\.\d+)?\b)")


def plan_query(request: SearchRequest) -> QueryPlan:
    normalized = " ".join(request.query.split())
    if not normalized:
        return QueryPlan("", False, True, (), "chunk")
    exact = bool(_EXACT_HINTS.search(normalized)) or len(normalized) <= 3
    semantic_words = any(word in normalized for word in ("为什么", "原因", "如何", "方案", "相关", "区别", "解释", "how", "why"))
    use_vector = request.entry in {"global", "ai"} and not (exact and not semantic_words)
    tree_hint = any(word in normalized for word in ("状态", "进展", "任务", "负责人", "阶段", "关系", "截止", "谁负责", "目前"))
    # Open-ended questions need the tree to route first, then scoped document
    # retrieval to supply the detailed answer. Keep fact-oriented questions
    # tree-first so short operational answers do not pull whole documents.
    broad_hint = any(word in normalized for word in ("总结", "综合", "所有", "整体", "分别", "对比", "哪些", "介绍", "了解", "多少", "具体", "内容", "机制", "资料", "信息"))
    mode = "fusion" if broad_hint else "tree" if tree_hint else "chunk"
    return QueryPlan(normalized, use_vector, exact, tuple(normalized.split()), mode)
