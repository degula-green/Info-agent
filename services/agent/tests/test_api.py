"""Agent HTTP API tests (in-memory store, no PostgreSQL or Redis required)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support import build_test_container, make_app

USER = {"X-Agent-User-Id": "user-1"}


def test_create_task_returns_accepted_with_events_url() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))

    response = client.post(
        "/api/agent/v1/tasks",
        json={"text": "read something", "steps": [{"capability": "fake.read", "arguments": {"value": "a"}}]},
        headers=USER,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "received"
    assert body["events_url"] == f"/api/agent/v1/tasks/{body['task_id']}/events"


def test_create_task_is_idempotent_on_client_message_id() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))

    first = client.post(
        "/api/agent/v1/tasks",
        json={"text": "hello", "client_message_id": "msg-1"},
        headers=USER,
    ).json()
    second = client.post(
        "/api/agent/v1/tasks",
        json={"text": "hello", "client_message_id": "msg-1"},
        headers=USER,
    ).json()

    assert first["task_id"] == second["task_id"]
    assert len(store.list_unfinished_tasks()) == 1


def test_task_is_scoped_to_owner() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    task_id = client.post("/api/agent/v1/tasks", json={"text": "hi"}, headers=USER).json()["task_id"]

    assert client.get(f"/api/agent/v1/tasks/{task_id}", headers=USER).status_code == 200
    assert client.get(f"/api/agent/v1/tasks/{task_id}", headers={"X-Agent-User-Id": "other"}).status_code == 403
    assert client.get("/api/agent/v1/tasks/missing", headers=USER).status_code == 404


def test_read_task_flow_exposes_plan_and_observations() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    task_id = client.post(
        "/api/agent/v1/tasks",
        json={"text": "read", "steps": [{"capability": "fake.read", "arguments": {"value": "a"}}]},
        headers=USER,
    ).json()["task_id"]

    run = client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=USER)
    assert run.status_code == 200
    assert run.json()["status"] == "succeeded"

    plan = client.get(f"/api/agent/v1/tasks/{task_id}/plan", headers=USER).json()["plan"]
    assert plan["status"] == "completed"
    observations = client.get(
        f"/api/agent/v1/tasks/{task_id}/observations", headers=USER
    ).json()["items"]
    assert [item["output"]["value"] for item in observations] == ["a"]


def test_write_task_requires_approval_via_api() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    task_id = client.post(
        "/api/agent/v1/tasks",
        json={"text": "write", "steps": [{"capability": "fake.write", "arguments": {"value": "x"}}]},
        headers=USER,
    ).json()["task_id"]

    assert client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=USER).json()["status"] == "waiting_approval"
    approvals = client.get("/api/agent/v1/approvals", headers=USER).json()["items"]
    assert len(approvals) == 1
    approval = approvals[0]

    approved = client.post(
        f"/api/agent/v1/approvals/{approval['approval_id']}/approve",
        json={"version": approval["version"]},
        headers=USER,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    assert client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=USER).json()["status"] == "succeeded"


def test_approval_rejects_stale_version() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    task_id = client.post(
        "/api/agent/v1/tasks",
        json={"text": "write", "steps": [{"capability": "fake.write", "arguments": {"value": "x"}}]},
        headers=USER,
    ).json()["task_id"]
    client.post(f"/api/agent/v1/tasks/{task_id}/run", headers=USER)
    approval = client.get("/api/agent/v1/approvals", headers=USER).json()["items"][0]

    stale = client.post(
        f"/api/agent/v1/approvals/{approval['approval_id']}/approve",
        json={"version": approval["version"] + 5},
        headers=USER,
    )
    assert stale.status_code == 409


def test_input_and_cancel_endpoints() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))

    waiting = client.post(
        "/api/agent/v1/tasks",
        json={"text": "need info", "steps": [{"capability": "fake.ask", "arguments": {}}]},
        headers=USER,
    ).json()["task_id"]
    client.post(f"/api/agent/v1/tasks/{waiting}/run", headers=USER)
    provided = client.post(
        f"/api/agent/v1/tasks/{waiting}/input", json={"text": "here it is"}, headers=USER
    )
    assert provided.status_code == 200
    assert provided.json()["status"] == "planning"

    cancelled = client.post(
        "/api/agent/v1/tasks",
        json={"text": "cancel me", "steps": [{"capability": "fake.read", "arguments": {"value": "a"}}]},
        headers=USER,
    ).json()["task_id"]
    response = client.post(f"/api/agent/v1/tasks/{cancelled}/cancel", headers=USER)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_health_endpoint() -> None:
    container, _store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    assert client.get("/health").json() == {"service": "agent", "status": "ok"}
