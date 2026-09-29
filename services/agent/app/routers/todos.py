"""The to-do surface the desktop talks to.

A to-do is created by the Agent (preview -> approval -> ``todo.create``) and
then belongs to the owner: from here they list it, edit it, finish it or
delete it. Nothing in this router is reachable by the runtime.

Deletion is a hard delete on purpose. The product decision is that the preview
disappears once the to-do is confirmed, and the desktop row disappears once it
is done -- there is no undo history to keep in v1.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from app.container import AgentContainer

router = APIRouter(prefix="/api/agent/v1")

_container: AgentContainer | None = None

TODO_STATUSES = ("open", "done", "cancelled")


def set_container(container: AgentContainer) -> None:
    global _container
    _container = container


def get_container() -> AgentContainer:
    if _container is None:
        raise HTTPException(status_code=503, detail="agent container is not initialised")
    return _container


class TodoCreateBody(BaseModel):
    """A to-do created directly from the desktop, without an Agent Task.

    The Agent path is the normal one; this exists so the desktop can add a
    reminder by hand and gets the exact same row shape back.
    """

    title: str = Field(min_length=1, max_length=200)
    due_at: str | None = None
    due_expression: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    client_message_id: str | None = Field(default=None, max_length=200)


class TodoUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: str | None = None
    due_expression: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in TODO_STATUSES:
            raise ValueError("status must be one of: " + ", ".join(TODO_STATUSES))
        return value


def _current_user(x_agent_user_id: str | None) -> str:
    container = get_container()
    user = (x_agent_user_id or "").strip()
    if user:
        return user
    return getattr(container.settings, "default_user_id", "") or "dev-user"


@router.get("/todos")
def list_todos(
    status: str = "",
    limit: int = Query(default=200, ge=1, le=500),
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    """Open items first; an overdue one is still open and still listed."""

    container = get_container()
    statuses = [item.strip() for item in status.split(",") if item.strip()]
    items = container.todo_store.list_todos(
        _current_user(x_agent_user_id), statuses=statuses or None, limit=limit
    )
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.post("/todos", status_code=201)
def create_todo(
    body: TodoCreateBody,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    """Creates a to-do without a Task; the same row the capability writes."""

    import uuid

    from app.capabilities.todo import TodoCreateCapability

    container = get_container()
    owner = _current_user(x_agent_user_id)
    key = f"desktop:{owner}:{body.client_message_id or uuid.uuid4()}"
    capability = TodoCreateCapability(
        container.todo_store, default_timezone=container.settings.default_timezone
    )
    result = capability.execute(
        capability.validate(
            {
                "title": body.title,
                "due_at": body.due_at,
                "due_expression": body.due_expression,
                "timezone": body.timezone,
                "notes": body.notes,
                "owner_user_id": owner,
                "idempotency_key": key,
            }
        )
    )
    todo = container.todo_store.get_todo(result["todo_id"])
    return todo.model_dump(mode="json") if todo else result


@router.get("/todos/{todo_id}")
def get_todo(
    todo_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    todo = container.todo_store.get_todo(todo_id)
    if todo is None or todo.owner_user_id != _current_user(x_agent_user_id):
        raise HTTPException(status_code=404, detail="unknown todo")
    return todo.model_dump(mode="json")


@router.patch("/todos/{todo_id}")
def update_todo(
    todo_id: str,
    body: TodoUpdateBody,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    """Partial update from the desktop (title, due time, status)."""

    container = get_container()
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    updated = container.todo_store.update_todo(
        todo_id, owner_user_id=_current_user(x_agent_user_id), changes=changes
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="unknown todo")
    return updated.model_dump(mode="json")


@router.delete("/todos/{todo_id}", status_code=204, response_class=Response)
def delete_todo(
    todo_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> Response:
    """The owner finished or dropped it; the row is removed for good."""

    container = get_container()
    deleted = container.todo_store.delete_todo(
        todo_id, owner_user_id=_current_user(x_agent_user_id)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="unknown todo")
    return Response(status_code=204)
