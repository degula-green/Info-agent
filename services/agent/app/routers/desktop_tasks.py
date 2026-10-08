"""Device-authenticated queue used by the Electron desktop client."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response

from app.container import AgentContainer
from app.infrastructure.knowledge.device_auth import DeviceAuthError, DeviceIdentity

router = APIRouter(prefix="/api/agent/v1/desktop")

_container: AgentContainer | None = None


def set_container(container: AgentContainer) -> None:
    global _container
    _container = container


def get_container() -> AgentContainer:
    if _container is None:
        raise HTTPException(status_code=503, detail="agent container is not initialised")
    return _container


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _task_payload(task) -> dict[str, Any]:
    return {
        "desktop_task_id": task.desktop_task_id,
        "agent_task_id": task.agent_task_id,
        "agent_step_id": task.agent_step_id,
        "device_id": task.device_id,
        "operation": task.operation,
        "request_payload": task.request_payload,
        "status": task.status,
        "result": task.result,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "attempt": task.attempt,
        "expires_at": _iso(task.expires_at),
        "delivered_at": _iso(task.delivered_at),
        "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
        "side_effect_state": task.side_effect_state,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
    }


async def _authenticate(
    request: Request,
    device_id: str,
) -> DeviceIdentity:
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")
    container = get_container()
    if container.device_auth is None:
        raise HTTPException(status_code=503, detail="device authentication is unavailable")
    body = await request.body()
    payload_hash = hashlib.sha256(body).hexdigest()
    try:
        return container.device_auth.verify_request(
            device_id=device_id,
            headers=request.headers,
            method=request.method,
            path=request.url.path,
            payload_hash=payload_hash,
        )
    except DeviceAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/tasks")
async def claim_desktop_task(
    request: Request,
    response: Response,
    device_id: str = Header(default="", alias="X-Agent-Device-ID"),
    wait_seconds: float = 0.0,
) -> dict[str, Any] | None:
    identity = await _authenticate(request, device_id)
    container = get_container()
    if container.desktop_tasks is None:
        raise HTTPException(status_code=503, detail="desktop task queue is unavailable")
    deadline = asyncio.get_running_loop().time() + min(
        max(0.0, float(wait_seconds)),
        30.0,
    )
    while True:
        task = container.desktop_tasks.claim_task(
            device_id=identity.device_id,
            lease_seconds=container.settings.desktop_task_lease_seconds,
        )
        if task is not None:
            return _task_payload(task)
        if asyncio.get_running_loop().time() >= deadline:
            response.status_code = 204
            return None
        await asyncio.sleep(0.5)


@router.post("/tasks/{desktop_task_id}/result")
async def complete_desktop_task(
    desktop_task_id: str,
    request: Request,
    device_id: str = Header(default="", alias="X-Agent-Device-ID"),
) -> dict[str, Any]:
    identity = await _authenticate(request, device_id)
    container = get_container()
    if container.desktop_tasks is None:
        raise HTTPException(status_code=503, detail="desktop task queue is unavailable")
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="result body must be JSON") from exc
    status = str(body.get("status") or "completed").strip().lower()
    if status not in {
        "completed",
        "failed",
        "expired",
        "waiting_login",
        "needs_review",
    }:
        raise HTTPException(status_code=400, detail="desktop task status is invalid")
    result = body.get("result")
    if result is not None and not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="result must be an object")
    task = container.desktop_tasks.complete_task(
        desktop_task_id=desktop_task_id,
        device_id=identity.device_id,
        status=status,
        result=result,
        error_code=str(body.get("error_code") or ""),
        error_message=str(body.get("error_message") or ""),
        side_effect_state=str(body.get("side_effect_state") or ""),
    )
    if task is None:
        raise HTTPException(status_code=409, detail="desktop task is already terminal or missing")
    return _task_payload(task)


@router.post("/tasks/{desktop_task_id}/heartbeat")
async def heartbeat_desktop_task(
    desktop_task_id: str,
    request: Request,
    device_id: str = Header(default="", alias="X-Agent-Device-ID"),
) -> dict[str, Any]:
    identity = await _authenticate(request, device_id)
    container = get_container()
    if container.desktop_tasks is None:
        raise HTTPException(status_code=503, detail="desktop task queue is unavailable")
    if not container.desktop_tasks.heartbeat_task(
        desktop_task_id=desktop_task_id,
        device_id=identity.device_id,
        lease_seconds=container.settings.desktop_task_lease_seconds,
    ):
        raise HTTPException(status_code=404, detail="desktop task was not found")
    return {"status": "ok"}
