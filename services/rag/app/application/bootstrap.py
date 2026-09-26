from __future__ import annotations

from functools import lru_cache

from app.application.callback_service import CallbackLane
from app.application.index_service import MVPIndexService
from app.application.memory_service import MemoryCandidateService
from app.application.parse_service import MVPParseService
from app.application.runtime import MVPWorkerRuntime
from app.application.rag_service import RAGRetrievalService
from app.config import settings
from app.infrastructure.embedding.client import EmbeddingClient
from app.infrastructure.module2.knowledge_client import Module2KnowledgeClient
from app.infrastructure.module2.rag_callback import KnowledgeRAGCallbackClient
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository, PostgresRagMVPRepository
from app.infrastructure.rag_elasticsearch import RagChunkIndex
from app.infrastructure.service1.rag_authorization import (
    AllowAllAuthorizationGateway,
    RagAuthorizationClient,
)
from app.infrastructure.storage.artifacts import build_artifact_store


def build_repository() -> PostgresRagMVPRepository | InMemoryRagMVPRepository:
    return PostgresRagMVPRepository() if settings.database_url else InMemoryRagMVPRepository()


def build_runtime() -> MVPWorkerRuntime:
    settings.validate_mvp()
    repository = build_repository()
    index = RagChunkIndex()
    embedding = EmbeddingClient()
    callback = CallbackLane(
        repository=repository,
        publisher=KnowledgeRAGCallbackClient(),
    )
    return MVPWorkerRuntime(
        repository=repository,
        parse_service=MVPParseService(
            knowledge=Module2KnowledgeClient(),
            artifact_store=build_artifact_store(),
        ),
        index_service=MVPIndexService(
            repository=repository,
            indexer=index,
            embedding=embedding,
        ),
        memory_service=MemoryCandidateService(repository=repository),
        callback_lane=callback,
    )


def build_retrieval_service() -> RAGRetrievalService:
    settings.validate_mvp()
    repository = build_repository()
    authorization = (
        RagAuthorizationClient()
        if settings.authz_base_url
        else AllowAllAuthorizationGateway()
    )
    return RAGRetrievalService(
        repository=repository,
        indexer=RagChunkIndex(),
        embedding=EmbeddingClient(),
        authorization=authorization,
    )
