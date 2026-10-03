from __future__ import annotations

from datetime import datetime, timezone

from app.application.conversation_memory import ConversationContextService
from app.application.conversation_memory_extraction import (
    ConversationMemoryExtractionService,
    USER_NAME_MEMORY_KEY,
)
from app.application.memory_service import MemoryService
from app.capabilities.chat_reply import ChatReplyCapability
from app.kernel.execution_context import ExecutionContext, bind_execution_context
from app.kernel.models import (
    ConversationContext,
    ConversationRecord,
    MemoryRecord,
    MessageRecord,
    TaskRecord,
    TaskUnderstanding,
)
from app.planning.knowledge import KnowledgeRoutingPlanner
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from tests.support import make_settings


def _task_with_message(
    store: InMemoryAgentStore,
    *,
    conversation_id: str,
    task_id: str,
    message_id: str,
    text: str,
) -> TaskRecord:
    moment = datetime.now(timezone.utc)
    conversation = ConversationRecord(
        conversation_id=conversation_id,
        owner_user_id="user-1",
        created_at=moment,
        updated_at=moment,
    )
    store.create_conversation(conversation)
    message = MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        role="user",
        content=text,
        status="completed",
        task_id=task_id,
        created_at=moment,
        updated_at=moment,
    )
    store.add_message(message)
    task = TaskRecord(
        task_id=task_id,
        source_type="chat",
        owner_user_id="user-1",
        status="succeeded",
        input={"text": text},
        conversation_id=conversation_id,
        request_message_id=message_id,
        created_at=moment,
        updated_at=moment,
    )
    store.tasks[task_id] = task
    return task


def test_explicit_self_introduction_is_written_and_retrieved_in_conversation() -> None:
    store = InMemoryAgentStore()
    task = _task_with_message(
        store,
        conversation_id="conversation-1",
        task_id="task-1",
        message_id="message-1",
        text="我是张三",
    )
    settings = make_settings(conversation_memory_enabled=True)
    memory_service = MemoryService(store)
    service = ConversationMemoryExtractionService(store, settings, memory_service)

    records = service.extract_after_task(task.task_id)

    assert len(records) == 1
    assert records[0].memory_key == USER_NAME_MEMORY_KEY
    assert records[0].content == "用户在会话中自称为“张三”"
    assert records[0].source_message_ids == ["message-1"]
    assert records[0].extraction_method == "explicit"

    context = ConversationContextService(
        store,
        settings,
        memory_service=memory_service,
    ).load(
        TaskRecord(
            task_id="task-2",
            source_type="chat",
            owner_user_id="user-1",
            status="planning",
            input={"text": "我是谁？"},
            conversation_id="conversation-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    assert context is not None
    assert [item.content for item in context.relevant_memories] == [
        "用户在会话中自称为“张三”"
    ]


def test_name_update_replaces_the_previous_active_memory() -> None:
    store = InMemoryAgentStore()
    settings = make_settings(conversation_memory_enabled=True)
    memory_service = MemoryService(store)
    service = ConversationMemoryExtractionService(store, settings, memory_service)

    first = _task_with_message(
        store,
        conversation_id="conversation-1",
        task_id="task-1",
        message_id="message-1",
        text="我是张三",
    )
    second = _task_with_message(
        store,
        conversation_id="conversation-1",
        task_id="task-2",
        message_id="message-2",
        text="我叫李四",
    )

    service.extract_after_task(first.task_id)
    service.extract_after_task(second.task_id)
    active = store.list_memories(
        "user-1",
        conversation_id="conversation-1",
        statuses=["active"],
    )

    assert len(active) == 1
    assert active[0].content == "用户在会话中自称为“李四”"


def test_explicit_memory_never_leaks_to_another_conversation() -> None:
    store = InMemoryAgentStore()
    settings = make_settings(conversation_memory_enabled=True)
    memory_service = MemoryService(store)
    service = ConversationMemoryExtractionService(store, settings, memory_service)
    task = _task_with_message(
        store,
        conversation_id="conversation-1",
        task_id="task-1",
        message_id="message-1",
        text="我是张三",
    )

    service.extract_after_task(task.task_id)
    context = ConversationContextService(
        store,
        settings,
        memory_service=memory_service,
    ).load(
        TaskRecord(
            task_id="task-2",
            source_type="chat",
            owner_user_id="user-1",
            status="planning",
            input={"text": "我是谁？"},
            conversation_id="conversation-2",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    assert context is None
    assert store.list_memories(
        "user-1",
        conversation_id="conversation-2",
    ) == []


def test_remember_command_writes_conversation_context_memory() -> None:
    store = InMemoryAgentStore()
    settings = make_settings(conversation_memory_enabled=True)
    memory_service = MemoryService(store)
    service = ConversationMemoryExtractionService(store, settings, memory_service)
    task = _task_with_message(
        store,
        conversation_id="conversation-1",
        task_id="task-1",
        message_id="message-1",
        text="记住，这个会话使用 PostgreSQL",
    )

    records = service.extract_after_task(task.task_id)

    assert len(records) == 1
    assert records[0].memory_type == "context"
    assert records[0].content == "这个会话使用 PostgreSQL"


def test_memory_recall_routes_to_chat_reply_not_knowledge_retrieval() -> None:
    from app.ingress.chat import ChatIngress

    task = ChatIngress().create_task("user-1", {"text": "我是张三，记住了吗？"})
    understanding = TaskUnderstanding(
        is_task=True,
        goal="确认用户身份并建立记忆",
        task_kind="answer",
        intent_candidates=[],
        confidence=0.95,
        reason="knowledge.answer",
    )
    planner = KnowledgeRoutingPlanner(object())
    descriptor = ChatReplyCapability(
        type("Provider", (), {"reply": lambda self, text: None})()
    ).descriptor

    plan = planner.create_plan(
        task,
        [descriptor],
        [],
        None,
        understanding,
    )

    assert len(plan.steps) == 1
    assert plan.steps[0].capability == "chat.reply"


def test_chat_reply_receives_bound_conversation_context() -> None:
    captured: dict[str, object] = {}

    class Provider:
        def reply(self, text: str, *, conversation_context=None):
            captured["text"] = text
            captured["context"] = conversation_context
            return type("Draft", (), {"reply": "我记得你是张三。", "model_calls": 1})()

    context = ConversationContext(
        conversation_id="conversation-1",
        summary="用户叫张三",
    )
    execution = ExecutionContext(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        owner_user_id="user-1",
        organization_id=None,
        request_id="request-1",
        trace_id="trace-1",
        source_type="chat",
        source_ref={},
        conversation_context=context,
    )
    capability = ChatReplyCapability(Provider())

    with bind_execution_context(execution):
        result = capability.execute(
            capability.validate({"text": "我是谁？"})
        )

    assert result["answer"] == "我记得你是张三。"
    assert captured["context"] is context


def test_chat_reply_answers_known_name_without_model_call() -> None:
    class Provider:
        def reply(self, text: str, *, conversation_context=None):
            raise AssertionError("known conversation memory should not call the model")

    memory = MemoryRecord(
        memory_id="memory-1",
        owner_user_id="user-1",
        memory_type="fact",
        title="当前会话中的用户名字",
        content="用户在会话中自称为“李四”",
        content_hash="hash",
        memory_key=USER_NAME_MEMORY_KEY,
        keywords=["李四"],
        source_conversation_id="conversation-1",
        extraction_method="explicit",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    context = ConversationContext(
        conversation_id="conversation-1",
        relevant_memories=[memory],
    )
    execution = ExecutionContext(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        owner_user_id="user-1",
        organization_id=None,
        request_id="request-1",
        trace_id="trace-1",
        source_type="chat",
        source_ref={},
        conversation_context=context,
    )
    capability = ChatReplyCapability(Provider())

    with bind_execution_context(execution):
        result = capability.execute(
            capability.validate({"text": "我是谁？"})
        )

    assert result["answer"] == "你在当前会话里说自己是“李四”。"
    assert result["model_calls"] == 0
