from __future__ import annotations

from fastapi import Depends, Request

from app.application.bootstrap import ApplicationContainer
from app.application.rag_service import RAGRetrievalService


def get_container(request: Request) -> ApplicationContainer:
    return request.app.state.container


def get_retrieval_service(
    container: ApplicationContainer = Depends(get_container),
) -> RAGRetrievalService:
    return container.retrieval_service
