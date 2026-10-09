"""Agent task API, including the Codex-style SSE event stream."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.application.task_service import (
    ConversationNotFoundError,
    TaskNotFoundError,
    TaskPermissionError,
    TaskStateError,
)
from app.auth import AuthenticatedUser, current_user, current_user_id
from app.container import AgentContainer
from app.infrastructure.attachment_store import RedisAttachmentStore
from app.infrastructure.web.desktop_form_browser_client import (
    DesktopFormBrowserClient,
    bind_desktop_form_owner,
)
from app.kernel.approval import ApprovalError
from app.kernel.errors import AgentContractError
from app.kernel.states import (
    EVENT_ANSWER_COMPLETED,
    EVENT_ANSWER_STARTED,
    EVENT_TASK_COMPLETED,
    TERMINAL_TASK_STATUSES,
    WAITING_TASK_STATUSES,
)

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
    conversation_id: UUID | None = None
    client_message_id: str | None = Field(default=None, max_length=128)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_ref: dict[str, Any] = Field(default_factory=dict)


class InputBody(BaseModel):
    text: str = Field(default="", max_length=8000)
    fields: dict[str, Any] = Field(default_factory=dict)
    resume: bool = False


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
    # A self report has to print a name, and the identity rows only carry
    # generated account labels, so the profile nickname travels with the task.
    owner_name = container.core_client.current_user_name(user.access_token)
    if owner_name:
        source_ref["owner_name"] = owner_name
    try:
        task = container.task_service.create_task(
            owner_user_id=user.user_id,
            source_type=body.source_type,
            payload=payload,
            source_ref=source_ref,
            constraints=body.constraints,
            client_message_id=body.client_message_id,
            conversation_id=(
                str(body.conversation_id) if body.conversation_id else None
            ),
            organization_id=organization_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "task_id": task.task_id,
        "status": task.status,
        "conversation_id": task.conversation_id,
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
            task_id,
            owner_user_id=owner_user_id,
            payload=payload,
            resume=body.resume,
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
    answer_id: str = "",
    answer_after: int = 0,
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
    current_answer_id = str(answer_id or "").strip()
    current_answer_after = max(0, int(answer_after or 0))

    async def generator() -> AsyncIterator[str]:
        nonlocal cursor, current_answer_id, current_answer_after
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.1)
        answer_stream = getattr(container, "answer_stream", None)
        pending_task_completed = None
        pending_answer_completed = None
        answer_completed_sent = False
        while True:
            # 1) Redis deltas first: the final result must never overtake the
            # tail of the answer.
            if current_answer_id and answer_stream is not None:
                try:
                    deltas = answer_stream.read_deltas(
                        task_id=task_id,
                        answer_id=current_answer_id,
                        after_seq=current_answer_after,
                    )
                except Exception:  # noqa: BLE001 - Redis is an optional path
                    deltas = []
                for delta in deltas or []:
                    seq = int(delta.get("seq") or 0)
                    if seq <= current_answer_after:
                        continue
                    current_answer_after = seq
                    yield _sse(
                        "answer.delta",
                        {
                            "sequence": 0,
                            "event_type": "answer.delta",
                            "task_id": task_id,
                            "payload": {
                                "answer_id": current_answer_id,
                                "step_id": str(delta.get("step_id") or ""),
                                "attempt": int(delta.get("attempt") or 0),
                                "seq": seq,
                                "offset": int(delta.get("offset") or 0),
                                "delta": str(delta.get("delta") or ""),
                            },
                        },
                    )
            # 2) PostgreSQL events, holding the two terminal markers.
            events = container.task_service.list_events(task_id, after_sequence=cursor)
            for event in events:
                cursor = event.sequence
                if event.event_type == EVENT_TASK_COMPLETED:
                    pending_task_completed = event
                    continue
                if event.event_type == EVENT_ANSWER_STARTED:
                    started_id = str(event.payload.get("answer_id") or "")
                    if started_id and started_id != current_answer_id:
                        current_answer_id = started_id
                        current_answer_after = 0
                        pending_answer_completed = None
                        answer_completed_sent = False
                    yield _sse(
                        event.event_type,
                        event.model_dump(mode="json"),
                        event_id=str(event.sequence),
                    )
                    continue
                if event.event_type == EVENT_ANSWER_COMPLETED:
                    pending_answer_completed = event
                    continue
                yield _sse(
                    event.event_type,
                    event.model_dump(mode="json"),
                    event_id=str(event.sequence),
                )

            # 3) Emit answer.completed only after the attempt is drained.
            redis_state: dict[str, Any] = {}
            if current_answer_id and answer_stream is not None:
                try:
                    redis_state = (
                        answer_stream.status(
                            task_id=task_id, answer_id=current_answer_id
                        )
                        or {}
                    )
                except Exception:  # noqa: BLE001 - Redis loss degrades to PG
                    redis_state = {}
            if current_answer_id and pending_answer_completed is not None:
                final_seq: int | None = int(
                    pending_answer_completed.payload.get("final_seq") or 0
                )
            elif current_answer_id and redis_state.get("done"):
                final_seq = int(redis_state.get("final_seq") or 0)
            else:
                final_seq = None
            redis_lost = bool(
                current_answer_id
                and pending_task_completed is not None
                and answer_stream is not None
                and not (redis_state.get("next_seq") or redis_state.get("done"))
            )
            if current_answer_id and answer_stream is None and pending_task_completed is not None:
                redis_lost = True
            if current_answer_id and not answer_completed_sent:
                answer_done = (
                    pending_answer_completed is not None
                    or bool(redis_state.get("done"))
                )
                if redis_lost:
                    # The transient buffer is gone; task.completed carries the
                    # final result, so do not block the stream on a drain.
                    answer_completed_sent = True
                elif answer_done and final_seq is not None and current_answer_after >= final_seq:
                    payload = (
                        dict(pending_answer_completed.payload or {})
                        if pending_answer_completed is not None
                        else {
                            "answer_id": current_answer_id,
                            "final_seq": final_seq,
                        }
                    )
                    if answer_stream is not None:
                        try:
                            snapshot = answer_stream.snapshot(
                                task_id=task_id, answer_id=current_answer_id
                            )
                        except Exception:  # noqa: BLE001 - degrade to PG data
                            snapshot = None
                        if snapshot:
                            payload["answer"] = snapshot.get("text", "")
                            payload["citations"] = snapshot.get("citations", [])
                            payload["warnings"] = snapshot.get(
                                "warnings", payload.get("warnings") or []
                            )
                    completed_sequence = (
                        pending_answer_completed.sequence
                        if pending_answer_completed is not None
                        else 0
                    )
                    yield _sse(
                        "answer.completed",
                        {
                            "sequence": completed_sequence,
                            "event_type": "answer.completed",
                            "task_id": task_id,
                            "payload": payload,
                        },
                        event_id=(
                            str(completed_sequence) if completed_sequence else None
                        ),
                    )
                    answer_completed_sent = True

            # 4) task.completed is the last event on the wire.
            if pending_task_completed is not None:
                if not current_answer_id or answer_completed_sent:
                    yield _sse(
                        pending_task_completed.event_type,
                        pending_task_completed.model_dump(mode="json"),
                        event_id=str(pending_task_completed.sequence),
                    )
                    return

            task = container.store.get_task(task_id)
            if task is None:
                return
            if task.status in TERMINAL_TASK_STATUSES or task.status in WAITING_TASK_STATUSES:
                if not events and pending_task_completed is None:
                    return
            if loop.time() >= deadline:
                return
            await asyncio.sleep(0.05)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}/answers/{answer_id}")
def get_answer_snapshot(
    task_id: str,
    answer_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Recover a streamed answer's text when the client detects a seq gap."""

    container = get_container()
    try:
        container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    answer_stream = getattr(container, "answer_stream", None)
    if answer_stream is None:
        return {
            "answer_id": answer_id,
            "text": "",
            "next_seq": 0,
            "completed": False,
            "final_seq": 0,
        }
    try:
        snapshot = answer_stream.snapshot(task_id=task_id, answer_id=answer_id)
    except Exception:  # noqa: BLE001 - the client falls back to task.completed
        snapshot = None
    if snapshot is None:
        return {
            "answer_id": answer_id,
            "text": "",
            "next_seq": 0,
            "completed": False,
            "final_seq": 0,
        }
    return snapshot


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
    if isinstance(container.form_browser, DesktopFormBrowserClient):
        raise HTTPException(
            status_code=409,
            detail="complete the sign-in in the desktop app and resume the task",
        )
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
    if isinstance(container.form_browser, DesktopFormBrowserClient):
        raise HTTPException(
            status_code=409,
            detail="enter the sign-in details in the desktop app",
        )
    try:
        return container.form_browser.send_input(
            str(takeover["session_id"]),
            body.model_dump(),
        )
    except AgentContractError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _latest_write(container: AgentContainer, task_id: str):
    """The most recent form.apply that actually wrote something."""

    for observation in reversed(container.task_service.list_observations(task_id)):
        output = observation.output or {}
        if observation.capability == "form.apply" and output.get("written_range"):
            return observation, output
    return None, None


@router.get("/tasks/{task_id}/form/undo")
def form_undo_state(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Whether the last write can be taken back, and where it landed."""

    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    _, output = _latest_write(container, task_id)
    if not output:
        return {"available": False}
    previous = output.get("previous") or []
    return {
        "available": bool(output.get("target")),
        "written_range": str(output.get("written_range") or ""),
        "target": str(output.get("target") or ""),
        "restores_to": previous,
    }


@router.post("/tasks/{task_id}/form/undo")
def form_undo(
    task_id: str,
    owner_user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Put the cells back the way they were before the write.

    The owner is clicking undo on a receipt for a write they just authorised,
    so this restores exactly the snapshot taken before that write. It is not a
    new approval: gating a revert behind a second approval would strand a
    mistake the owner is looking straight at.
    """

    container = get_container()
    container.task_service.get_task(task_id, owner_user_id=owner_user_id)
    if container.form_browser is None:
        raise HTTPException(status_code=503, detail="form browser is not configured")
    observation, output = _latest_write(container, task_id)
    if not output:
        raise HTTPException(status_code=409, detail="this task wrote nothing to undo")

    target = str(output.get("target") or "")
    written_range = str(output.get("written_range") or "")
    previous = [
        [str(cell) for cell in row]
        for row in (output.get("previous") or [])
        if isinstance(row, list)
    ]
    if not target or not written_range:
        raise HTTPException(status_code=409, detail="the write did not record where it landed")

    step = container.store.get_step(observation.step_id)
    draft = ((step.arguments or {}).get("draft") if step is not None else None) or {}
    url = str(draft.get("form_url") or "")
    if not url:
        raise HTTPException(status_code=409, detail="the write did not record its page")

    with bind_desktop_form_owner(owner_user_id):
        session_id = container.form_browser.create_session()
        try:
            container.form_browser.open(session_id, url)
            blank = not any(str(cell).strip() for row in previous for cell in row)
            if blank:
                # Nothing was there before, so undoing means clearing the block.
                container.form_browser.clear_range(session_id, written_range)
                return {"reverted": True, "target": target, "observed": [], "verified": True}
            width = max((len(row) for row in previous), default=1) or 1
            rows = [list(row) + [""] * (width - len(row)) for row in previous]
            written = container.form_browser.write_grid(session_id, target, rows)
        except AgentContractError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            container.form_browser.close_session(session_id)
    return {
        "reverted": True,
        "target": target,
        "observed": written.get("observed") or [],
        "verified": bool(written.get("verified")),
    }
