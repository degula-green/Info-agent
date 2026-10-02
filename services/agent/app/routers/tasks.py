"""Agent task API, including the Codex-style SSE event stream."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.application.task_service import TaskNotFoundError, TaskPermissionError, TaskStateError
from app.auth import AuthenticatedUser, current_user, current_user_id
from app.container import AgentContainer
from app.infrastructure.attachment_store import RedisAttachmentStore
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
    attachment_ids: list[str] = Field(default_factory=list)  # 新增
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


def get_attachment_store() -> RedisAttachmentStore:
    """获取附件存储实例"""
    import redis
    container = get_container()
    redis_url = (container.settings.redis_url or "").strip()
    if not redis_url:
        raise HTTPException(status_code=503, detail="attachment storage is not configured")
    r = redis.from_url(redis_url)
    return RedisAttachmentStore(r, container.settings.attachment_ttl_hours)


@router.post("/tasks", status_code=202)
def create_task(
    body: CreateTaskBody,
    user: AuthenticatedUser = Depends(current_user),
) -> dict[str, Any]:
    """Accepts a chat message or a collected message; the understanding layer decides.

    There is no keyword gate here for chat the way there is for collected text.
    A typed message is the owner talking to the Agent on purpose, so the only
    input that never becomes a Task is one that carries nothing at all: a
    message that is only the client-side "steps" of a legacy caller, with no
    text to understand.
    """

    container = get_container()
    if not (body.text or "").strip() and not body.steps:
        raise HTTPException(status_code=422, detail="text or steps is required")
    payload: dict[str, Any] = {"text": body.text}

    # 校验附件所有权
    if body.attachment_ids:
        owner_id = user.user_id
        # Only attachment-bearing tasks need the attachment store. Building it
        # eagerly made every plain chat turn depend on Redis.
        store = get_attachment_store()
        for att_id in body.attachment_ids:
            if not store.validate_ownership(att_id, owner_id):
                raise HTTPException(403, f"无权使用附件: {att_id}")
        payload["attachment_ids"] = body.attachment_ids

    if body.steps:
        payload["steps"] = body.steps
    source_ref = dict(body.source_ref)
    source_ref.pop("organization_id", None)
    organization_id = container.core_client.current_organization(user.access_token)
    if organization_id:
        source_ref["organization_id"] = organization_id
    task = container.task_service.create_task(
        owner_user_id=user.user_id,
        source_type=body.source_type,
        payload=payload,
        source_ref=source_ref,
        constraints=body.constraints,
        client_message_id=body.client_message_id,
    )
    return {
        "task_id": task.task_id,
        "status": task.status,
        "events_url": f"/api/agent/v1/tasks/{task.task_id}/events",
    }


@router.get("/tasks")
def list_tasks(
    status: str = "",
    limit: int = 50,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Lists the caller's Tasks so a client can discover fan-out and completed work."""

    container = get_container()
    statuses = [item.strip() for item in status.split(",") if item.strip()]
    items = container.task_service.list_tasks(
        owner_user_id=owner_user_id,
        statuses=statuses or None,
        limit=min(max(limit, 1), 200),
    )
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    try:
        task = container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return task.model_dump(mode="json")


@router.get("/tasks/{task_id}/plan")
def get_plan(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    plan = container.task_service.get_active_plan(task_id)
    return {"plan": plan.model_dump(mode="json") if plan else None}


@router.get("/tasks/{task_id}/observations")
def list_observations(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    observations = container.task_service.list_observations(task_id)
    return {"items": [item.model_dump(mode="json") for item in observations]}


@router.get("/approvals")
def list_approvals(
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    items = container.store.list_approvals(owner_user_id=owner_user_id)
    payload: list[dict[str, Any]] = []
    for item in items:
        body = item.model_dump(mode="json")
        capability = container.registry.find(item.capability)
        # The preview card has to warn *before* the owner confirms an action
        # whose outcome cannot be read back afterwards. The registry is the only
        # trusted source for that, so it is resolved here instead of being
        # denormalised onto every approval row.
        body["reconcilable"] = bool(
            capability is not None and capability.descriptor.reconcilable
        )
        payload.append(body)
    return {"items": payload}


@router.post("/approvals/{approval_id}/approve")
def approve(
    approval_id: str,
    body: DecisionBody,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    try:
        approval = container.execution_service.approval_gateway.decide(
            approval_id,
            owner_user_id=owner_user_id,
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
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    try:
        approval = container.execution_service.approval_gateway.decide(
            approval_id,
            owner_user_id=owner_user_id,
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
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    payload: dict[str, Any] = {"text": body.text} if body.text else {}
    payload.update(body.fields)
    try:
        task = container.task_service.submit_input(
            task_id, owner_user_id=owner_user_id, payload=payload
        )
    except (TaskNotFoundError, TaskPermissionError, TaskStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task.model_dump(mode="json")


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    try:
        task = container.task_service.cancel(task_id, owner_user_id=owner_user_id)
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
    owner_user_id: str = Depends(current_user_id),
) -> StreamingResponse:
    container = get_container()
    try:
        container.task_service.get_task(task_id, owner_user_id=owner_user_id)
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
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Synchronous drive used by operators and tests; the worker is the normal path."""

    container = get_container()
    try:
        container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        status = container.execution_service.run_task(task_id)
    except AgentContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": status}


class TakeoverInputBody(BaseModel):
    """One owner action forwarded into the takeover browser."""

    model_config = {"extra": "forbid"}

    kind: str = Field(pattern="^(click|type|key|scroll)$")
    x: float | None = None
    y: float | None = None
    text: str = Field(default="", max_length=2000)
    delta_x: float = 0
    delta_y: float = 0


def _takeover_session(container: AgentContainer, task_id: str) -> dict[str, Any] | None:
    """The live browser a paused Task is waiting on, if any.

    A capability that ran into a login wall records the session it was holding
    on its Observation; that is what the owner's takeover drives.
    """

    for observation in reversed(container.task_service.list_observations(task_id)):
        output = observation.output or {}
        takeover = output.get("takeover")
        if isinstance(takeover, dict) and takeover.get("session_id"):
            return takeover
    return None


def _require_takeover(container: AgentContainer, task_id: str) -> dict[str, Any]:
    takeover = _takeover_session(container, task_id)
    if takeover is None:
        raise HTTPException(status_code=409, detail="this task has no takeover session")
    if container.form_browser is None:
        raise HTTPException(status_code=503, detail="form browser is not configured")
    return takeover


@router.get("/tasks/{task_id}/takeover")
def get_takeover(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Whether this Task is waiting on the owner to sign in, and where."""

    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    takeover = _takeover_session(container, task_id)
    if takeover is None:
        return {"required": False, "url": "", "session_id": ""}
    return {
        "required": True,
        "url": str(takeover.get("url") or ""),
        "session_id": str(takeover.get("session_id") or ""),
    }


@router.get("/tasks/{task_id}/takeover/screenshot")
def takeover_screenshot(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> Response:
    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    takeover = _require_takeover(container, task_id)
    try:
        image = container.form_browser.screenshot(str(takeover["session_id"]))
    except AgentContractError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/tasks/{task_id}/takeover/input")
def takeover_input(
    task_id: str,
    body: TakeoverInputBody,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    takeover = _require_takeover(container, task_id)
    try:
        return container.form_browser.send_input(
            str(takeover["session_id"]),
            body.model_dump(),
        )
    except AgentContractError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
