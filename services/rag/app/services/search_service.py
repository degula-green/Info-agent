from __future__ import annotations

import time
import uuid
import hashlib
from dataclasses import dataclass
from typing import Any

from app.application.retrieval.context_assembler import assemble_context
from app.application.retrieval.query_planner import plan_query
from app.application.retrieval.fulltext_retriever import FullTextRetriever
from app.application.retrieval.hybrid_retriever import HybridRetriever, RetrievalDiagnostics
from app.application.ports import AuthorizationGateway, EmbeddingProvider
from app.config import settings
from app.domain.models import SearchRequest, SearchResult
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.elasticsearch import ElasticsearchChunkStore, ElasticsearchUnavailable
from app.infrastructure.qa import OpenAICompatibleAnswerProvider, QAUnavailable
from app.infrastructure.rerank import RemoteReranker
from app.infrastructure.service1.authorization_client import AllowAllAuthorizationGateway, Service1AuthorizationClient
from app.infrastructure.persistence.repository import InMemoryRagRepository, PostgresRagRepository
from app.services.memory_search_service import get_tree_search_service
from app.domain.memory import TreeSearchRequest


@dataclass
class SearchResponse:
    results: list[SearchResult]
    diagnostics: RetrievalDiagnostics
    request_id: str


@dataclass
class PreparedAnswer:
    request: SearchRequest
    conversation_id: str
    user_message_id: str
    started: float
    response: SearchResponse
    results: list[SearchResult]
    context: Any
    diagnostics: dict[str, Any]
    retrieval_mode: str
    tree_diagnostics: dict[str, Any]


class QAHistoryUnavailable(RuntimeError):
    """The answer could not be durably associated with a QA conversation."""


class QAConversationNotFound(LookupError):
    """The requested conversation is not active or belongs to another user."""


class RagSearchService:
    def __init__(
        self,
        *,
        store: Any | None = None,
        authorization: AuthorizationGateway | None = None,
        embedding: EmbeddingProvider | None = None,
        repository: Any | None = None,
        tree_search_service: Any | None = None,
        answer_provider: Any | None = None,
    ) -> None:
        self.store = store
        self.authorization = authorization
        self.embedding = embedding
        self.repository = repository or (PostgresRagRepository() if settings.database_url else InMemoryRagRepository())
        self.tree_search_service = tree_search_service
        self.answer_provider = answer_provider
        self._hybrid: HybridRetriever | None = None

    def _engine(self) -> HybridRetriever:
        if self._hybrid is None:
            store = self.store or ElasticsearchChunkStore()
            if self.authorization is not None:
                authorization = self.authorization
            elif settings.authz_base_url:
                authorization = Service1AuthorizationClient()
            elif settings.environment in {"development", "test"}:
                # Local development has no Core/OpenFGA endpoint in the
                # default .env. Keep display retrieval usable while the
                # gateway still fails closed for protected content.
                authorization = AllowAllAuthorizationGateway()
            else:
                authorization = Service1AuthorizationClient()
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
                filters={"entry": request.entry, "knowledge_base_id": request.knowledge_base_id, "knowledge_base_ids": list(request.knowledge_base_ids)},
                result_count=len(results),
                duration_ms=duration_ms,
                request_id=request_id,
            )
        except Exception:
            # Search availability must not depend on an audit/history write.
            pass
        return SearchResponse(results, diagnostics, request_id)

    def answer(self, request: SearchRequest, *, defer_generation: bool = False) -> dict[str, Any] | PreparedAnswer:
        request = SearchRequest(**{**request.__dict__, "entry": "ai"})
        # Resolve and record the user turn before retrieval. This guarantees
        # that model/search failures still leave a durable failed assistant
        # message, while an arbitrary conversation_id can never cross users.
        conversation_id = self._resolve_conversation(request)
        user_message_id = self._add_message(conversation_id=conversation_id, role="user", content=request.query)
        started = time.perf_counter()
        plan = plan_query(request)
        # Apply the same deterministic UTC bounds to any scoped Chunk fallback
        # that follows Tree navigation.  Invalid/unresolved expressions stay
        # unfiltered and are reported in diagnostics.
        if plan.time_resolved:
            request = SearchRequest(**{**request.__dict__, "occurred_after": plan.time_after, "occurred_before": plan.time_before})
        # Deep controls candidate/context budgets only.  It no longer turns a
        # tree-first question into an unscoped parallel fusion search.
        mode = plan.retrieval_mode
        response = SearchResponse(
            [],
            RetrievalDiagnostics(plan, False, 0, 0, 0, ("qa_not_started",)),
            uuid.uuid4().hex,
        )
        final_results: list[SearchResult] = []
        context = assemble_context([])
        tree_diagnostics: dict[str, Any] = {}
        answer: str | None = None
        error_code: str | None = None
        error_message: str | None = None
        try:
            chunk_response = SearchResponse(
                [], RetrievalDiagnostics(plan, False, 0, 0, 0, ("chunk_not_started",)), uuid.uuid4().hex,
            )
            base_ids = tuple(request.knowledge_base_ids) or ((request.knowledge_base_id,) if request.knowledge_base_id else ())
            tree_items: list[dict[str, Any]] = []
            source_attachment_ids: set[str] = set()
            source_knowledge_item_ids: set[str] = set()
            pending_source_count = 0
            failed_source_count = 0
            fallback_level = 0
            fallback_reason: str | None = None
            if base_ids:
                tree_service = self.tree_search_service or get_tree_search_service()
                tree = tree_service.search(TreeSearchRequest(
                    query=request.query, user_id=request.user_id, organization_id=request.organization_id,
                    knowledge_base_id=base_ids[0], knowledge_base_ids=base_ids,
                    tree_types=(plan.preferred_tree_type,),
                    occurred_after=plan.time_after, occurred_before=plan.time_before,
                    top_k=min(50, request.top_k * (2 if request.qa_mode == "deep" else 1)),
                    include_protected=request.include_protected,
                    expand_context=True,
                    context_window_minutes=30 if request.qa_mode == "deep" else 10,
                    context_limit=12 if request.qa_mode == "deep" else 6,
                ))
                tree_diagnostics = tree.get("diagnostics") or {}
                tree_diagnostics.update({"routing": tree.get("routing") or {}, "context_diagnostics": tree.get("context_diagnostics") or {}})
                tree_items = list(tree.get("items") or [])
                for item in tree_items:
                    for source in item.get("sources") or []:
                        processing_status = str((source.get("source_metadata") or {}).get("processing_status") or "ready")
                        if processing_status == "pending":
                            pending_source_count += 1
                        elif processing_status == "failed":
                            failed_source_count += 1
                        if source.get("attachment_id"):
                            source_attachment_ids.add(str(source["attachment_id"]))
                        if source.get("knowledge_item_id"):
                            source_knowledge_item_ids.add(str(source["knowledge_item_id"]))
                    # Facts are navigation metadata.  Only authorized direct
                    # and neighboring Chunks become formal prompt evidence.
                    for context_chunk in item.get("context_chunks") or []:
                        if not isinstance(context_chunk, dict) or not str(context_chunk.get("content") or "").strip():
                            continue
                        fact = item.get("fact") or {}
                        protected = str(context_chunk.get("visibility") or fact.get("visibility") or "display") == "protected"
                        attachment_id = context_chunk.get("attachment_id") or fact.get("attachment_id")
                        knowledge_item_id = context_chunk.get("knowledge_item_id") or fact.get("knowledge_item_id")
                        auth_type = context_chunk.get("auth_resource_type") or ("attachment" if protected and attachment_id else "knowledge_item")
                        auth_part = context_chunk.get("auth_resource_part") or ("content" if protected and attachment_id else "display")
                        auth_id = context_chunk.get("auth_resource_id") or attachment_id or knowledge_item_id or ""
                        final_results.append(SearchResult(
                            chunk_id=str(context_chunk.get("chunk_id") or uuid.uuid4()),
                            content=str(context_chunk.get("content") or ""), score=float(item.get("score") or 0),
                            rank=len(final_results) + 1,
                            source={**context_chunk, "knowledge_item_id": knowledge_item_id,
                                   "knowledge_base_id": context_chunk.get("knowledge_base_id") or fact.get("knowledge_base_id"),
                                   "organization_id": context_chunk.get("organization_id") or fact.get("organization_id"),
                                   "attachment_id": attachment_id, "auth_resource_type": auth_type,
                                   "auth_resource_part": auth_part, "auth_resource_id": auth_id,
                                   "fact_id": fact.get("fact_id"), "fact_version_id": fact.get("fact_version_id"),
                                   "tree": item.get("tree"), "tree_path": item.get("path"),
                                   "evidence_relation": context_chunk.get("relation") or "direct_evidence", "rag_eligible": True},
                        ))
            needs_document_evidence = not final_results
            if needs_document_evidence and (source_attachment_ids or source_knowledge_item_ids):
                scoped_request = SearchRequest(**{
                    **request.__dict__,
                    # The QA conversation id is a history record id, not the
                    # source message's conversation_group_id. Passing it into
                    # chunk filters incorrectly excludes attachments when the
                    # user asks a follow-up in an existing QA conversation.
                    "conversation_id": None,
                    "source_attachment_ids": tuple(sorted(source_attachment_ids)),
                    "source_knowledge_item_ids": tuple(sorted(source_knowledge_item_ids)),
                })
                chunk_response = self.search(scoped_request)
                final_results.extend(chunk_response.results)
            if needs_document_evidence and not chunk_response.results and base_ids:
                fallback_level = 2
                fallback_reason = "tree_sources_empty" if not (source_attachment_ids or source_knowledge_item_ids) else "source_chunks_empty"
                # Keep the fallback inside the caller supplied knowledge-base
                # and organization scope.  Never widen a tree miss globally.
                chunk_response = self.search(SearchRequest(**{**request.__dict__, "conversation_id": None}))
                final_results.extend(chunk_response.results)
            if needs_document_evidence and not chunk_response.results and not base_ids:
                fallback_level = 3
                fallback_reason = "unscoped_chunk_search"
                chunk_response = self.search(SearchRequest(**{**request.__dict__, "conversation_id": None}))
                final_results.extend(chunk_response.results)
            tree_diagnostics = {
                **tree_diagnostics,
                "retrieval_stage": "tree_evidence" if final_results and tree_items else "scoped_chunk_fallback",
                "fallback_level": fallback_level,
                "fallback_reason": fallback_reason,
                "tree_candidate_count": len(tree_items),
                "source_candidate_count": len(source_attachment_ids | source_knowledge_item_ids),
                "chunk_candidate_count": len(chunk_response.results),
                "pending_source_count": pending_source_count,
                "failed_source_count": failed_source_count,
                "execution_path": "tree_evidence" if final_results and tree_items else "scoped_chunk_fallback" if base_ids else "chunk",
                "source_processing_notice": (
                    "相关资料已采集，但文档内容仍在处理中，暂不能引用正文。"
                    if pending_source_count else
                    "相关资料已采集，但文档解析失败，暂不能引用正文。"
                    if failed_source_count else None
                ),
            }
            final_results = _dedupe_results(final_results, request.top_k * (2 if request.qa_mode == "deep" else 1))
            response = SearchResponse(final_results, chunk_response.diagnostics, chunk_response.request_id)
            # The retrieval-stage check protects ranking. Re-check immediately
            # before prompt construction because ACLs may change between those
            # two operations.
            final_results = self._engine().authorize_results(request, response.results)
            context = assemble_context(final_results)
            if defer_generation:
                return PreparedAnswer(
                    request=request, conversation_id=conversation_id, user_message_id=user_message_id,
                    started=started, response=response, results=final_results, context=context,
                    diagnostics={**_diagnostics(response.diagnostics), **tree_diagnostics},
                    retrieval_mode=mode, tree_diagnostics=tree_diagnostics,
                )
            provider = self.answer_provider or OpenAICompatibleAnswerProvider()
            answer = provider.generate(request.query, final_results)
        except QAUnavailable as exc:
            error_code, error_message = "qa_unavailable", str(exc)
        except ElasticsearchUnavailable as exc:
            error_code, error_message = "search_unavailable", str(exc)
        except Exception as exc:
            error_code, error_message = "qa_unavailable", type(exc).__name__

        return self._finish_answer(
            conversation_id=conversation_id, user_message_id=user_message_id, started=started,
            response=response, context=context, results=final_results,
            diagnostics={**_diagnostics(response.diagnostics), **tree_diagnostics}, retrieval_mode=mode,
            tree_diagnostics=tree_diagnostics, answer=answer, error_code=error_code, error_message=error_message,
        )

    def prepare_answer(self, request: SearchRequest) -> PreparedAnswer | dict[str, Any]:
        return self.answer(request, defer_generation=True)

    def complete_answer(self, prepared: PreparedAnswer, answer: str) -> dict[str, Any]:
        return self._finish_answer(
            conversation_id=prepared.conversation_id, user_message_id=prepared.user_message_id,
            started=prepared.started, response=prepared.response, context=prepared.context,
            results=prepared.results, diagnostics=prepared.diagnostics,
            retrieval_mode=prepared.retrieval_mode, tree_diagnostics=prepared.tree_diagnostics,
            answer=answer, error_code=None, error_message=None,
        )

    def fail_answer(self, prepared: PreparedAnswer, error: Exception) -> dict[str, Any]:
        return self._finish_answer(
            conversation_id=prepared.conversation_id, user_message_id=prepared.user_message_id,
            started=prepared.started, response=prepared.response, context=prepared.context,
            results=prepared.results, diagnostics=prepared.diagnostics,
            retrieval_mode=prepared.retrieval_mode, tree_diagnostics=prepared.tree_diagnostics,
            answer=None, error_code="qa_unavailable", error_message=str(error),
        )

    def _finish_answer(self, *, conversation_id: str, user_message_id: str, started: float, response: SearchResponse, context: Any, results: list[SearchResult], diagnostics: dict[str, Any], retrieval_mode: str, tree_diagnostics: dict[str, Any], answer: str | None, error_code: str | None, error_message: str | None) -> dict[str, Any]:
        duration_ms = int((time.perf_counter() - started) * 1000)
        assistant_message_id = self._add_message(
            conversation_id=conversation_id, role="assistant", content=answer or "", citations=context.citations,
            model_name=settings.qa_model, prompt_version="v1", status="completed" if answer else "failed",
            duration_ms=duration_ms, error_message=None if answer else error_message or "AI 问答暂时不可用",
        )
        return {
            "request_id": response.request_id, "conversation_id": conversation_id,
            "user_message_id": user_message_id, "assistant_message_id": assistant_message_id,
            "answer": answer, "error_code": error_code, "context": context.text,
            "citations": context.citations, "items": [item.as_dict() for item in results],
            "diagnostics": diagnostics, "retrieval_mode": retrieval_mode,
            "execution_path": tree_diagnostics.get("execution_path") or (tree_diagnostics.get("routing") or {}).get("execution_path") or "chunk",
            "tree_diagnostics": tree_diagnostics,
        }

    def _resolve_conversation(self, request: SearchRequest) -> str:
        try:
            if request.conversation_id:
                try:
                    uuid.UUID(str(request.conversation_id))
                except (TypeError, ValueError):
                    raise QAConversationNotFound("conversation not found")
                value = self.repository.get_qa_conversation(user_id=request.user_id, conversation_id=str(request.conversation_id))
                if not value:
                    raise QAConversationNotFound("conversation not found")
                return str(request.conversation_id)
            return self.repository.create_qa_conversation(
                user_id=request.user_id,
                organization_id=request.organization_id,
                title=request.query[:300],
                retrieval_mode=request.qa_mode or "quick",
                knowledge_base_ids=list(request.knowledge_base_ids) or ([request.knowledge_base_id] if request.knowledge_base_id else []),
            )
        except QAConversationNotFound:
            raise
        except Exception as exc:
            raise QAHistoryUnavailable("QA conversation could not be persisted") from exc

    def _add_message(self, **kwargs: Any) -> str:
        try:
            return str(self.repository.add_qa_message(**kwargs))
        except Exception as exc:
            raise QAHistoryUnavailable("QA message could not be persisted") from exc


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
        "retrieval_mode": value.plan.retrieval_mode,
    }


def _dedupe_results(results: list[SearchResult], limit: int) -> list[SearchResult]:
    seen: set[str] = set()
    document_counts: dict[str, int] = {}
    output: list[SearchResult] = []
    for result in sorted(results, key=lambda item: item.score, reverse=True):
        key = result.chunk_id
        if key in seen or not result.content.strip():
            continue
        is_tree_fact = bool(result.source.get("fact_id"))
        document_key = result.attachment_id or result.knowledge_item_id
        if not is_tree_fact and document_key:
            count = document_counts.get(document_key, 0)
            if count >= settings.max_chunks_per_item:
                continue
            document_counts[document_key] = count + 1
        seen.add(key)
        result.rank = len(output) + 1
        output.append(result)
        if len(output) >= max(1, limit):
            break
    return output
