"""Conversation-scoped memory API."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.application.memory_service import (
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryValidationError,
)
from app.auth import current_user_id
from app.container import AgentContainer

router = APIRouter(prefix="/api/agent/v1")

_container: AgentContainer | None = None


def set_container(container: AgentContainer) -> None:
    global _container
    _container = container


def get_container() -> AgentContainer:
    if _container is None:
        raise HTTPException(status_code=503, detail="agent container is not initialised")
    return _container


class MemoryCreateBody(BaseModel):
    memory_type: Literal["fact", "decision", "relation", "context"]
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=8000)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)


@router.get("/conversations/{conversation_id}/memories")
def list_memories(
    conversation_id: UUID,
    memory_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    owner_user_id: str = Depends(current_user_id),
) -> dict:
    container = get_container()
    try:
        items = container.memory_service.list(
            owner_user_id=owner_user_id,
            conversation_id=str(conversation_id),
            memory_type=memory_type,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        total = container.memory_service.count(
            owner_user_id=owner_user_id,
            conversation_id=str(conversation_id),
            memory_type=memory_type,
        )
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MemoryPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except MemoryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.post("/conversations/{conversation_id}/memories", status_code=201)
def create_memory(
    conversation_id: UUID,
    body: MemoryCreateBody,
    owner_user_id: str = Depends(current_user_id),
) -> dict:
    container = get_container()
    try:
        memory = container.memory_service.create(
            owner_user_id=owner_user_id,
            conversation_id=str(conversation_id),
            memory_type=body.memory_type,
            title=body.title,
            content=body.content,
            keywords=body.keywords,
            importance=body.importance,
            confidence=body.confidence,
        )
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MemoryPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except MemoryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return memory.model_dump(mode="json")


@router.delete(
    "/conversations/{conversation_id}/memories/{memory_id}",
    status_code=204,
)
def delete_memory(
    conversation_id: UUID,
    memory_id: UUID,
    owner_user_id: str = Depends(current_user_id),
) -> Response:
    container = get_container()
    try:
        container.memory_service.delete(
            owner_user_id=owner_user_id,
            conversation_id=str(conversation_id),
            memory_id=str(memory_id),
        )
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MemoryPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return Response(status_code=204)
