from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.application.conversation_memory import ConversationContextService
from app.application.memory_service import MemoryService
from app.kernel.models import ConversationRecord, TaskRecord
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from tests.support import build_test_container, make_app, make_settings

USER = {"Authorization": "Bearer user-1-token"}
OTHER_USER = {"Authorization": "Bearer user-2-token"}


def _conversation(store: InMemoryAgentStore, *, conversation_id: str, owner: str):
    moment = datetime.now(timezone.utc)
    conversation = ConversationRecord(
        conversation_id=conversation_id,
        owner_user_id=owner,
        created_at=moment,
        updated_at=moment,
    )
    store.create_conversation(conversation)
    return conversation


def test_memory_api_is_conversation_scoped_and_owner_isolated() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    conversation = client.post(
        "/api/agent/v1/conversations",
        json={"title": "记忆测试"},
        headers=USER,
    ).json()
    conversation_id = conversation["conversation_id"]

    created = client.post(
        f"/api/agent/v1/conversations/{conversation_id}/memories",
        json={
            "memory_type": "fact",
            "title": "部署环境",
            "content": "青云官网部署在阿里云。",
            "keywords": ["青云官网", "阿里云"],
            "importance": 0.8,
        },
        headers=USER,
    )

    assert created.status_code == 201
    body = created.json()
    assert body["scope"] == "conversation"
    assert body["status"] == "active"
    assert body["source_conversation_id"] == conversation_id

    listed = client.get(
        f"/api/agent/v1/conversations/{conversation_id}/memories",
        headers=USER,
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert (
        client.get(
            f"/api/agent/v1/conversations/{conversation_id}/memories",
            headers=OTHER_USER,
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/agent/v1/conversations/{conversation_id}/memories/{body['memory_id']}",
            headers=OTHER_USER,
        ).status_code
        == 403
    )

    preference = client.post(
        f"/api/agent/v1/conversations/{conversation_id}/memories",
        json={
            "memory_type": "preference",
            "title": "偏好",
            "content": "更喜欢简短回答",
        },
        headers=USER,
    )
    assert preference.status_code == 422

    removed = client.delete(
        f"/api/agent/v1/conversations/{conversation_id}/memories/{body['memory_id']}",
        headers=USER,
    )
    assert removed.status_code == 204
    assert store.list_memories(
        "user-1",
        conversation_id=conversation_id,
    ) == []


def test_delete_conversation_cascades_memory() -> None:
    container, store, _publisher, _registry = build_test_container()
    client = TestClient(make_app(container))
    conversation_id = client.post(
        "/api/agent/v1/conversations",
        json={"title": "cascade"},
        headers=USER,
    ).json()["conversation_id"]
    memory = client.post(
        f"/api/agent/v1/conversations/{conversation_id}/memories",
        json={
            "memory_type": "decision",
            "title": "选型",
            "content": "使用 PostgreSQL。",
        },
        headers=USER,
    ).json()

    assert client.delete(
        f"/api/agent/v1/conversations/{conversation_id}",
        headers=USER,
    ).status_code == 204
    assert store.get_memory(memory["memory_id"]) is None


def test_context_retrieval_never_crosses_conversation() -> None:
    store = InMemoryAgentStore()
    memory_service = MemoryService(store)
    first = _conversation(store, conversation_id="conversation-1", owner="user-1")
    second = _conversation(store, conversation_id="conversation-2", owner="user-1")
    memory_service.create(
        owner_user_id="user-1",
        conversation_id=first.conversation_id,
        memory_type="fact",
        title="当前会话事实",
        content="青云官网部署在阿里云。",
        keywords=["青云官网", "阿里云"],
        importance=0.9,
    )
    memory_service.create(
        owner_user_id="user-1",
        conversation_id=second.conversation_id,
        memory_type="fact",
        title="其他会话事实",
        content="另外一个项目部署在腾讯云。",
        keywords=["腾讯云"],
        importance=0.9,
    )
    context_service = ConversationContextService(
        store,
        make_settings(
            conversation_context_enabled=True,
            conversation_memory_enabled=True,
            conversation_memory_top_k=5,
        ),
        memory_service=memory_service,
    )
    moment = datetime.now(timezone.utc)
    task = TaskRecord(
        task_id="task-1",
        source_type="chat",
        owner_user_id="user-1",
        status="planning",
        input={"text": "青云官网部署在哪里"},
        conversation_id=first.conversation_id,
        created_at=moment,
        updated_at=moment,
    )

    context = context_service.load(task)

    assert context is not None
    assert [item.title for item in context.relevant_memories] == ["当前会话事实"]
