"""Agent task API, including the Codex-style SSE event stream."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.application.task_service import TaskNotFoundError, TaskPermissionError, TaskStateError
from app.container import AgentContainer
from app.kernel.approval import ApprovalError
from app.kernel.errors import AgentContractError
from app.kernel.states import TERMINAL_TASK_STATUSES, WAITING_TASK_STATUSES

router = APIRouter(prefix="/api/agent/v1")

_container: AgentContainer | None = None


def set_container(container: AgentContainer) -> None:
    global _container
    _container = container


def get_container() -> AgentContainer:
    if _container is None:
        raise HTTPException(status_code=503, detail="agent container is not initialised")
    return _container


class CreateTaskBody(BaseModel):
    text: str = Field(default="", max_length=8000)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    source_type: str = "chat"
    client_message_id: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_ref: dict[str, Any] = Field(default_factory=dict)


class InputBody(BaseModel):
    text: str = Field(default="", max_length=8000)
    fields: dict[str, Any] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    version: int | None = None
    arguments: dict[str, Any] | None = None


def _current_user(x_agent_user_id: str | None) -> str:
    container = get_container()
    user = (x_agent_user_id or "").strip()
    if user:
        return user
    fallback = getattr(container.settings, "default_user_id", "") or "dev-user"
    return fallback


@router.post("/tasks", status_code=202)
def create_task(
    body: CreateTaskBody,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    payload: dict[str, Any] = {"text": body.text}
    if body.steps:
        payload["steps"] = body.steps
    task = container.task_service.create_task(
        owner_user_id=_current_user(x_agent_user_id),
        source_type=body.source_type,
        payload=payload,
        source_ref=body.source_ref,
        constraints=body.constraints,
        client_message_id=body.client_message_id,
    )
    return {
        "task_id": task.task_id,
        "status": task.status,
        "events_url": f"/api/agent/v1/tasks/{task.task_id}/events",
    }


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    try:
        task = container.task_service.get_task(task_id, owner_user_id=_current_user(x_agent_user_id))
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return task.model_dump(mode="json")


@router.get("/tasks/{task_id}/plan")
def get_plan(
    task_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=_current_user(x_agent_user_id))
    plan = container.task_service.get_active_plan(task_id)
    return {"plan": plan.model_dump(mode="json") if plan else None}


@router.get("/tasks/{task_id}/observations")
def list_observations(
    task_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=_current_user(x_agent_user_id))
    observations = container.task_service.list_observations(task_id)
    return {"items": [item.model_dump(mode="json") for item in observations]}


@router.get("/approvals")
def list_approvals(
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    items = container.store.list_approvals(owner_user_id=_current_user(x_agent_user_id))
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.post("/approvals/{approval_id}/approve")
def approve(
    approval_id: str,
    body: DecisionBody,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    try:
        approval = container.execution_service.approval_gateway.decide(
            approval_id,
            owner_user_id=_current_user(x_agent_user_id),
            approve=True,
            version=body.version,
            arguments=body.arguments,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval.model_dump(mode="json")


@router.post("/approvals/{approval_id}/reject")
def reject(
    approval_id: str,
    body: DecisionBody,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    try:
        approval = container.execution_service.approval_gateway.decide(
            approval_id,
            owner_user_id=_current_user(x_agent_user_id),
            approve=False,
            version=body.version,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval.model_dump(mode="json")


@router.post("/tasks/{task_id}/input")
def submit_input(
    task_id: str,
    body: InputBody,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    payload: dict[str, Any] = {"text": body.text} if body.text else {}
    payload.update(body.fields)
    try:
        task = container.task_service.submit_input(
            task_id, owner_user_id=_current_user(x_agent_user_id), payload=payload
        )
    except (TaskNotFoundError, TaskPermissionError, TaskStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task.model_dump(mode="json")


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    container = get_container()
    try:
        task = container.task_service.cancel(task_id, owner_user_id=_current_user(x_agent_user_id))
    except (TaskNotFoundError, TaskPermissionError, TaskStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task.model_dump(mode="json")


def _sse(event_type: str, data: dict[str, Any], *, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    request: Request,
    after: int = 0,
    timeout_seconds: float = 30.0,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> StreamingResponse:
    container = get_container()
    try:
        container.task_service.get_task(task_id, owner_user_id=_current_user(x_agent_user_id))
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    last_event_id = request.headers.get("last-event-id")
    cursor = after
    if last_event_id and last_event_id.strip().isdigit():
        cursor = max(cursor, int(last_event_id.strip()))

    async def generator() -> AsyncIterator[str]:
        nonlocal cursor
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.1)
        while True:
            events = container.task_service.list_events(task_id, after_sequence=cursor)
            for event in events:
                cursor = event.sequence
                yield _sse(
                    event.event_type,
                    event.model_dump(mode="json"),
                    event_id=str(event.sequence),
                )
            task = container.store.get_task(task_id)
            if task is None:
                return
            if task.status in TERMINAL_TASK_STATUSES or task.status in WAITING_TASK_STATUSES:
                if not events:
                    return
            if loop.time() >= deadline:
                return
            await asyncio.sleep(0.05)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{task_id}/run")
def run_task_now(
    task_id: str,
    x_agent_user_id: str | None = Header(default=None, alias="X-Agent-User-Id"),
) -> dict[str, Any]:
    """Synchronous drive used by operators and tests; the worker is the normal path."""

    container = get_container()
    try:
        container.task_service.get_task(task_id, owner_user_id=_current_user(x_agent_user_id))
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        status = container.execution_service.run_task(task_id)
    except AgentContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": status}
