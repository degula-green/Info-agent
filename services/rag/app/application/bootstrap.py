from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.application.callback_service import CallbackLane
from app.application.branch_refresh_service import BranchRefreshService
from app.application.entity_review_service import EntityReviewService
from app.application.index_service import MVPIndexService
from app.application.location_verifier import LLMEntityVerifier
from app.application.memory_service import MemoryCandidateService
from app.application.parse_service import MVPParseService
from app.application.qa_service import QAService
from app.application.runtime import MVPWorkerRuntime
from app.application.rag_service import RAGRetrievalService
from app.application.tree_metrics_service import TreeMetricsService
from app.application.window_scan_service import EntityWindowScanWorker
from app.infrastructure.extraction.client import EntityExtractionClient
from app.config import settings
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.module2.knowledge_client import Module2KnowledgeClient
from app.infrastructure.module2.rag_callback import KnowledgeRAGCallbackClient
from app.infrastructure.persistence.mvp import (
    InMemoryRagMVPRepository,
    PostgresRagMVPRepository,
)
from app.infrastructure.rag_elasticsearch import RagChunkIndex
from app.infrastructure.qa import OpenAICompatibleAnswerProvider
from app.infrastructure.service1.rag_authorization import (
    AllowAllAuthorizationGateway,
    RagAuthorizationClient,
)
from app.infrastructure.storage.artifacts import build_artifact_store


@dataclass
class ApplicationContainer:
    repository: PostgresRagMVPRepository | InMemoryRagMVPRepository
    indexer: RagChunkIndex
    embedding: EmbeddingClient
    authorization: Any
    retrieval_service: RAGRetrievalService
    qa_service: QAService
    entity_review_service: EntityReviewService
    tree_metrics_service: TreeMetricsService

    def close(self) -> None:
        close = getattr(self.repository, "close", None)
        if callable(close):
            close()


def build_repository() -> PostgresRagMVPRepository | InMemoryRagMVPRepository:
    return (
        PostgresRagMVPRepository()
        if settings.database_url
        else InMemoryRagMVPRepository()
    )


def _build_authorization() -> Any:
    if settings.authz_base_url:
        return RagAuthorizationClient()
    if settings.development_like:
        return AllowAllAuthorizationGateway()
    raise RuntimeError("authorization configuration is required outside development/test")


def build_container() -> ApplicationContainer:
    settings.validate_mvp()
    repository = build_repository()
    indexer = RagChunkIndex()
    embedding = EmbeddingClient()
    authorization = _build_authorization()
    # L4 is opt-in: it adds a model round trip to the query path, so it stays
    # off until the escalation rate has been measured.
    verifier = LLMEntityVerifier() if settings.locate_llm_enabled else None
    retrieval = RAGRetrievalService(
        repository=repository,
        indexer=indexer,
        embedding=embedding,
        authorization=authorization,
        verifier=verifier,
    )
    qa_service = QAService(
        repository=repository,
        retrieval_service=retrieval,
        answer_provider=OpenAICompatibleAnswerProvider(),
    )
    entity_review_service = EntityReviewService(repository=repository)
    tree_metrics_service = TreeMetricsService(repository=repository)
    return ApplicationContainer(
        repository=repository,
        indexer=indexer,
        embedding=embedding,
        authorization=authorization,
        retrieval_service=retrieval,
        qa_service=qa_service,
        entity_review_service=entity_review_service,
        tree_metrics_service=tree_metrics_service,
    )


def build_runtime() -> MVPWorkerRuntime:
    container = build_container()
    callback = CallbackLane(
        repository=container.repository,
        publisher=KnowledgeRAGCallbackClient(),
    )
    runtime = MVPWorkerRuntime(
        repository=container.repository,
        parse_service=MVPParseService(
            knowledge=Module2KnowledgeClient(),
            artifact_store=build_artifact_store(),
        ),
        index_service=MVPIndexService(
            repository=container.repository,
            indexer=container.indexer,
            embedding=container.embedding,
        ),
        memory_service=MemoryCandidateService(repository=container.repository),
        callback_lane=callback,
        branch_refresh_service=BranchRefreshService(
            repository=container.repository,
            indexer=container.indexer,
        ),
        window_scan_service=EntityWindowScanWorker(
            repository=container.repository,
            extractor=EntityExtractionClient(),
        ),
    )
    runtime.container = container
    return runtime


def build_retrieval_service() -> RAGRetrievalService:
    return build_container().retrieval_service
