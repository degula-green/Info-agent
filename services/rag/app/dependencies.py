from __future__ import annotations

from fastapi import Depends, Request

from app.application.bootstrap import ApplicationContainer
from app.application.entity_review_service import EntityReviewService
from app.application.qa_service import QAService
from app.application.rag_service import RAGRetrievalService


def get_container(request: Request) -> ApplicationContainer:
    return request.app.state.container


def get_retrieval_service(
    container: ApplicationContainer = Depends(get_container),
) -> RAGRetrievalService:
    return container.retrieval_service


def get_qa_service(
    container: ApplicationContainer = Depends(get_container),
) -> QAService:
    return container.qa_service


def get_entity_review_service(
    container: ApplicationContainer = Depends(get_container),
) -> EntityReviewService:
    return container.entity_review_service
