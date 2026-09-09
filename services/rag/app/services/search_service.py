from __future__ import annotations

import time
import uuid
import hashlib
from dataclasses import dataclass
from typing import Any

from app.application.retrieval.context_assembler import assemble_context
from app.application.retrieval.fulltext_retriever import FullTextRetriever
from app.application.retrieval.hybrid_retriever import HybridRetriever, RetrievalDiagnostics
from app.application.ports import AuthorizationGateway, EmbeddingProvider
from app.config import settings
from app.domain.models import SearchRequest, SearchResult
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.elasticsearch import ElasticsearchChunkStore
from app.infrastructure.qa import OpenAICompatibleAnswerProvider, QAUnavailable
from app.infrastructure.rerank import RemoteReranker
from app.infrastructure.service1.authorization_client import Service1AuthorizationClient
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository


@dataclass
class SearchResponse:
    results: list[SearchResult]
    diagnostics: RetrievalDiagnostics
    request_id: str


class RagSearchService:
    def __init__(
        self,
        *,
        store: Any | None = None,
        authorization: AuthorizationGateway | None = None,
        embedding: EmbeddingProvider | None = None,
        repository: Any | None = None,
    ) -> None:
        self.store = store
        self.authorization = authorization
        self.embedding = embedding
        self.repository = repository or (PostgresRagRepository() if settings.database_url else InMemoryRagRepository())
        self._hybrid: HybridRetriever | None = None

    def _engine(self) -> HybridRetriever:
        if self._hybrid is None:
            store = self.store or ElasticsearchChunkStore()
            authorization = self.authorization or Service1AuthorizationClient()
            embedding = self.embedding or EmbeddingClient()
            self._hybrid = HybridRetriever(store=store, authorization=authorization, embedding=embedding, reranker=RemoteReranker() if settings.rerank_enabled else None)
        return self._hybrid

    def search(self, request: SearchRequest) -> SearchResponse:
        started = time.perf_counter()
        engine = self._engine()
        if request.entry == "knowledge":
            results, diagnostics = FullTextRetriever(engine).retrieve(request)
        else:
            results, diagnostics = engine.retrieve(request)
        request_id = uuid.uuid4().hex
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            self.repository.record_search(
                user_id=request.user_id,
                organization_id=request.organization_id,
                query_text=request.query,
                query_hash=hashlib.sha256(request.query.strip().encode("utf-8")).hexdigest(),
                filters={"entry": request.entry, "knowledge_base_id": request.knowledge_base_id},
                result_count=len(results),
                duration_ms=duration_ms,
                request_id=request_id,
            )
        except Exception:
            # Search availability must not depend on an audit/history write.
            pass
        return SearchResponse(results, diagnostics, request_id)

    def answer(self, request: SearchRequest) -> dict[str, Any]:
        request = SearchRequest(**{**request.__dict__, "entry": "ai"})
        response = self.search(request)
        # The retrieval-stage check protects ranking. Re-check immediately
        # before prompt construction because ACLs may change between those
        # two operations.
        final_results = self._engine().authorize_results(request, response.results)
        context = assemble_context(final_results)
        answer_provider = OpenAICompatibleAnswerProvider()
        try:
            answer = answer_provider.generate(request.query, final_results)
        except QAUnavailable:
            answer = None
        conversation_id = request.conversation_id
        user_message_id = None
        assistant_message_id = None
        try:
            if not conversation_id:
                conversation_id = self.repository.create_qa_conversation(user_id=request.user_id, organization_id=request.organization_id, title=request.query[:300])
            user_message_id = self.repository.add_qa_message(conversation_id=conversation_id, role="user", content=request.query)
            if answer is not None:
                assistant_message_id = self.repository.add_qa_message(conversation_id=conversation_id, role="assistant", content=answer, citations=context.citations, model_name=settings.qa_model, prompt_version="v1")
        except Exception:
            # QA persistence is best-effort and must not expose content through
            # an error response when the optional database is unavailable.
            pass
        return {
            "request_id": response.request_id,
            "conversation_id": conversation_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "answer": answer,
            "context": context.text,
            "citations": context.citations,
            "items": [item.as_dict() for item in final_results],
            "diagnostics": _diagnostics(response.diagnostics),
        }


_default_service: RagSearchService | None = None


def get_service() -> RagSearchService:
    global _default_service
    if _default_service is None:
        _default_service = RagSearchService()
    return _default_service


def search(query: str, *, user_id: str = "", organization_id: str | None = None, knowledge_base_id: str | None = None, entry: str = "global", top_k: int | None = None) -> dict[str, Any]:
    request = SearchRequest(
        query=query,
        user_id=user_id,
        organization_id=organization_id,
        knowledge_base_id=knowledge_base_id,
        entry=entry,
        top_k=top_k or settings.final_top_k,
    )
    response = get_service().search(request)
    return {
        "request_id": response.request_id,
        "query": query,
        "index": settings.elasticsearch_url,
        "items": [item.as_dict() for item in response.results],
        "diagnostics": _diagnostics(response.diagnostics),
    }


def _diagnostics(value: RetrievalDiagnostics) -> dict[str, Any]:
    return {
        "protected_scope_available": value.protected_scope_available,
        "protected_scope_count": value.protected_scope_count,
        "candidate_count": value.candidate_count,
        "authorized_count": value.authorized_count,
        "degraded": list(value.degraded),
        "use_vector": value.plan.use_vector,
    }
