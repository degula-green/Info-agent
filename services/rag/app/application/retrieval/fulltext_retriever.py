from __future__ import annotations

from app.application.retrieval.hybrid_retriever import HybridRetriever
from app.domain.models import SearchRequest


class FullTextRetriever:
    """Knowledge-library search: BM25 only, with the same authorization gate."""

    def __init__(self, hybrid: HybridRetriever) -> None:
        self.hybrid = hybrid

    def retrieve(self, request: SearchRequest):
        # The hybrid engine's planner skips vectorization for this entry.
        request = SearchRequest(**{**request.__dict__, "entry": "knowledge"})
        return self.hybrid.retrieve(request)
