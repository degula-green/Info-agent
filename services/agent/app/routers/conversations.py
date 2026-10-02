"""User-visible Agent conversation history.

Conversations and messages are owned by the Agent service. The router only
serves the caller's own history; Task execution detail remains on the Task API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUser, current_user, current_user_id
from app.container import AgentContainer
from app.kernel.events import utcnow
from app.kernel.models import ConversationRecord

router = APIRouter(prefix="/api/agent/v1")

_container: AgentContainer | None = None

CONVERSATION_STATUSES = ("active", "archived")


def set_container(container: AgentContainer) -> None:
    global _container
    _container = container


def get_container() -> AgentContainer:
    if _container is None:
        raise HTTPException(status_code=503, detail="agent container is not initialised")
    return _container


class ConversationCreateBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = None


def _owned_conversation(
    conversation_id: UUID, owner_user_id: str
) -> ConversationRecord:
    container = get_container()
    conversation = container.store.get_conversation(str(conversation_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if conversation.owner_user_id != owner_user_id:
        raise HTTPException(
            status_code=403,
            detail="conversation does not belong to the current user",
        )
    return conversation


@router.post("/conversations", status_code=201)
def create_conversation(
    body: ConversationCreateBody,
    user: AuthenticatedUser = Depends(current_user),
) -> dict[str, Any]:
    container = get_container()
    moment = utcnow()
    title = " ".join((body.title or "").split()) or "新的对话"
    conversation = ConversationRecord(
        conversation_id=str(uuid4()),
        owner_user_id=user.user_id,
        organization_id=container.core_client.current_organization(
            user.access_token
        ),
        title=title,
        created_at=moment,
        updated_at=moment,
    )
    stored = container.store.create_conversation(conversation)
    return {**stored.model_dump(mode="json"), "message_count": 0}


@router.get("/conversations")
def list_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str = "",
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    statuses = [item.strip() for item in status.split(",") if item.strip()]
    if any(item not in CONVERSATION_STATUSES for item in statuses):
        raise HTTPException(status_code=422, detail="unsupported conversation status")

    items = container.store.list_conversations_for_owner(
        owner_user_id,
        statuses=statuses or None,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    total = container.store.count_conversations_for_owner(
        owner_user_id,
        statuses=statuses or None,
    )
    return {
        "items": [
            {
                "conversation_id": item.conversation_id,
                "title": item.title,
                "status": item.status,
                "last_message_at": item.last_message_at,
                "message_count": container.store.count_messages(
                    item.conversation_id
                ),
            }
            for item in items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: UUID,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    conversation = _owned_conversation(conversation_id, owner_user_id)
    messages = container.store.list_messages(conversation.conversation_id)
    return {
        **conversation.model_dump(mode="json"),
        "message_count": len(messages),
        "messages": [message.model_dump(mode="json") for message in messages],
    }


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: UUID,
    body: ConversationUpdateBody,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    conversation = _owned_conversation(conversation_id, owner_user_id)
    if body.status is not None and body.status not in CONVERSATION_STATUSES:
        raise HTTPException(status_code=422, detail="unsupported conversation status")
    if body.title is None and body.status is None:
        raise HTTPException(status_code=422, detail="title or status is required")

    if body.title is not None:
        conversation.title = " ".join(body.title.split())
    if body.status is not None:
        conversation.status = body.status
    conversation.updated_at = utcnow()
    container.store.save_conversation(conversation)
    return {
        **conversation.model_dump(mode="json"),
        "message_count": container.store.count_messages(
            conversation.conversation_id
        ),
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: UUID,
    owner_user_id: str = Depends(current_user_id),
) -> Response:
    container = get_container()
    _owned_conversation(conversation_id, owner_user_id)
    container.store.delete_conversation(
        str(conversation_id), owner_user_id=owner_user_id
    )
    return Response(status_code=204)
