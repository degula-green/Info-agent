"""Phase 1 conversation history API and Task-to-Message projection."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.kernel.models import MessageRecord
from tests.support import build_test_container, make_app

USER = {"Authorization": "Bearer user-1-token"}
OTHER_USER = {"Authorization": "Bearer user-2-token"}


def test_task_without_conversation_creates_one_turn_atomically() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))

    response = client.post(
        "/api/agent/v1/tasks",
        json={"text": "青云官网部署到哪一步了？", "client_message_id": "msg-1"},
        headers=USER,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["conversation_id"]
    task = store.get_task(body["task_id"])
    assert task.conversation_id == body["conversation_id"]
    assert task.request_message_id
    assert task.response_message_id

    conversation = store.get_conversation(body["conversation_id"])
    assert conversation.title == "青云官网部署到哪一步了？"
    messages = store.list_messages(body["conversation_id"])
    assert [(item.role, item.status) for item in messages] == [
        ("user", "completed"),
        ("assistant", "pending"),
    ]
    assert messages[0].task_id == body["task_id"]
    assert messages[1].task_id == body["task_id"]


def test_task_idempotency_reuses_conversation_and_messages() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    payload = {
        "text": "重复发送",
        "client_message_id": "same-client-message",
    }

    first = client.post("/api/agent/v1/tasks", json=payload, headers=USER).json()
    second = client.post("/api/agent/v1/tasks", json=payload, headers=USER).json()

    assert first["task_id"] == second["task_id"]
    assert first["conversation_id"] == second["conversation_id"]
    assert len(store.list_conversations_for_owner("user-1")) == 1
    assert len(store.list_messages(first["conversation_id"])) == 2


def test_conversation_owner_isolation() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    created = client.post(
        "/api/agent/v1/conversations",
        json={"title": "private"},
        headers=USER,
    ).json()
    conversation_id = created["conversation_id"]
    store.add_message(
        MessageRecord(
            message_id="00000000-0000-0000-0000-000000000001",
            conversation_id=conversation_id,
            role="user",
            content="hello",
            status="completed",
            created_at=created["created_at"],
            updated_at=created["updated_at"],
        )
    )

    assert (
        client.get(
            f"/api/agent/v1/conversations/{conversation_id}",
            headers=OTHER_USER,
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/agent/v1/conversations/{conversation_id}",
            headers=OTHER_USER,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/agent/v1/tasks",
            json={"text": "steal", "conversation_id": conversation_id},
            headers=OTHER_USER,
        ).status_code
        == 403
    )
    assert store.get_conversation(conversation_id).owner_user_id == "user-1"


def test_conversation_list_detail_patch_and_delete() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))

    first = client.post(
        "/api/agent/v1/tasks",
        json={"text": "first", "client_message_id": "first"},
        headers=USER,
    ).json()
    second = client.post(
        "/api/agent/v1/conversations",
        json={"title": "second"},
        headers=USER,
    ).json()

    listed = client.get(
        "/api/agent/v1/conversations?page=1&page_size=1",
        headers=USER,
    ).json()
    assert listed["total"] == 2
    assert listed["page"] == 1
    assert listed["page_size"] == 1
    assert len(listed["items"]) == 1

    conversation_id = first["conversation_id"]
    detail = client.get(
        f"/api/agent/v1/conversations/{conversation_id}",
        headers=USER,
    ).json()
    assert detail["message_count"] == 2
    assert [item["role"] for item in detail["messages"]] == [
        "user",
        "assistant",
    ]

    patched = client.patch(
        f"/api/agent/v1/conversations/{second['conversation_id']}",
        json={"title": "renamed", "status": "archived"},
        headers=USER,
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "renamed"
    assert patched.json()["status"] == "archived"

    deleted = client.delete(
        f"/api/agent/v1/conversations/{conversation_id}",
        headers=USER,
    )
    assert deleted.status_code == 204
    assert store.get_conversation(conversation_id) is None
    assert store.list_messages(conversation_id) == []


def test_terminal_task_updates_assistant_message() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    created = client.post(
        "/api/agent/v1/tasks",
        json={
            "text": "read",
            "steps": [{"capability": "fake.read", "arguments": {"value": "a"}}],
        },
        headers=USER,
    ).json()

    run = client.post(
        f"/api/agent/v1/tasks/{created['task_id']}/run",
        headers=USER,
    )

    assert run.json()["status"] == "succeeded"
    messages = store.list_messages(created["conversation_id"])
    assert messages[1].status == "completed"
    assert messages[1].content == "任务已完成"
