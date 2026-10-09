from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.infrastructure.knowledge.device_auth import DeviceIdentity
from app.infrastructure.postgres.desktop_tasks import InMemoryDesktopTaskStore
from app.infrastructure.web.desktop_form_browser_client import (
    DesktopFormBrowserClient,
)
from app.kernel.execution_context import ExecutionContext, bind_execution_context
from app.routers import desktop_tasks


class FakeDeviceAuth:
    def get_active_device(self, owner_user_id: str) -> DeviceIdentity:
        return DeviceIdentity(
            device_id="device-1",
            owner_user_id=owner_user_id,
            connector_id="connector-1",
            expires_at="2027-01-01T00:00:00Z",
        )

    def verify_request(self, **_kwargs) -> DeviceIdentity:
        return self.get_active_device("u1")


class CompletingStore(InMemoryDesktopTaskStore):
    def __init__(self, results: dict[str, dict]) -> None:
        super().__init__()
        self.results = results
        self.operations: list[str] = []

    def get_task(self, desktop_task_id: str):
        task = super().get_task(desktop_task_id)
        if task is None:
            return None
        if task.status == "pending":
            self.operations.append(task.operation)
            result = self.results.get(task.operation, {})
            status = "waiting_login" if result.get("_waiting_login") else "completed"
            clean_result = {
                key: value for key, value in result.items() if key != "_waiting_login"
            }
            return self.complete_task(
                desktop_task_id=desktop_task_id,
                device_id=task.device_id,
                status=status,
                result=clean_result,
            )
        return task


def execution_context() -> ExecutionContext:
    return ExecutionContext(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        owner_user_id="u1",
        organization_id="org-1",
        request_id="request-1",
        trace_id="trace-1",
        source_type="chat",
        source_ref={},
    )


def test_desktop_form_browser_routes_atomic_operations() -> None:
    store = CompletingStore(
        {
            "create_session": {"session_id": "local-session"},
            "open": {"kind": "form", "final_url": "https://example.com/form"},
            "read_form": {"fields": [{"label": "姓名"}]},
            "submit_form": {"verified": True},
        }
    )
    client = DesktopFormBrowserClient(
        store,
        FakeDeviceAuth(),
        task_ttl_seconds=30,
        poll_seconds=0.01,
        timeout_seconds=5,
    )
    with bind_execution_context(execution_context()):
        session_id = client.create_session()
        opened = client.open(session_id, "https://example.com/form")
        form = client.read_form(session_id)
        submitted = client.submit_form(session_id)
    assert session_id == "local-session"
    assert opened["kind"] == "form"
    assert form["fields"][0]["label"] == "姓名"
    assert submitted["verified"] is True
    assert store.operations == ["create_session", "open", "read_form", "submit_form"]


def test_desktop_form_browser_surfaces_local_login_requirement() -> None:
    store = CompletingStore(
        {
            "create_session": {"session_id": "local-session"},
            "open": {
                "_waiting_login": True,
                "final_url": "https://example.com/login",
                "session_id": "local-session",
            },
        }
    )
    client = DesktopFormBrowserClient(
        store,
        FakeDeviceAuth(),
        task_ttl_seconds=30,
        poll_seconds=0.01,
        timeout_seconds=5,
    )
    with bind_execution_context(execution_context()):
        session_id = client.create_session()
        opened = client.open(session_id, "https://example.com/login")
    assert opened["login_required"] is True
    assert opened["session_id"] == "local-session"
    assert opened["final_url"] == "https://example.com/login"


def test_desktop_task_store_claim_and_complete_is_idempotent() -> None:
    store = InMemoryDesktopTaskStore()
    task = store.create_task(
        device_id="device-1",
        operation="read_form",
        request_payload={"session_id": "s1"},
        ttl_seconds=30,
    )
    claimed = store.claim_task(device_id="device-1", lease_seconds=10)
    assert claimed is not None
    assert claimed.desktop_task_id == task.desktop_task_id
    completed = store.complete_task(
        desktop_task_id=task.desktop_task_id,
        device_id="device-1",
        status="completed",
        result={"ok": True},
    )
    assert completed is not None
    assert completed.completed_at is not None
    assert (
        store.complete_task(
            desktop_task_id=task.desktop_task_id,
            device_id="device-1",
            status="completed",
            result={"ok": True},
        )
        is None
    )


def test_desktop_task_api_claims_and_completes_a_task() -> None:
    store = InMemoryDesktopTaskStore()
    created = store.create_task(
        device_id="device-1",
        operation="read_form",
        request_payload={"session_id": "s1"},
        ttl_seconds=30,
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(desktop_task_lease_seconds=30),
        desktop_tasks=store,
        device_auth=FakeDeviceAuth(),
    )
    desktop_tasks.set_container(container)
    app = FastAPI()
    app.include_router(desktop_tasks.router)
    client = TestClient(app)
    headers = {
        "X-Agent-Device-ID": "device-1",
        "X-Agent-Device-Key": "device-key",
        "X-Agent-Timestamp": "1700000000",
        "X-Agent-Payload-Hash": "0" * 64,
        "X-Agent-Signature": "signature",
    }
    claimed = client.get("/api/agent/v1/desktop/tasks", headers=headers)
    assert claimed.status_code == 200
    assert claimed.json()["desktop_task_id"] == created.desktop_task_id
    completed = client.post(
        f"/api/agent/v1/desktop/tasks/{created.desktop_task_id}/result",
        headers=headers,
        json={"status": "completed", "result": {"ok": True}},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
