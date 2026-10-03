from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.application.conversation_memory import (
    ConversationContextService,
    ConversationSummaryService,
)
from app.kernel.execution_context import (
    ExecutionContext,
    bind_execution_context,
    bind_conversation_context,
)
from app.kernel.models import (
    ConversationContext,
    ConversationRecord,
    MessageRecord,
    TaskRecord,
)
from app.kernel.runtime import AgentRuntime
from app.kernel.registry import CapabilityRegistry
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from tests.support import make_settings


def _conversation(store: InMemoryAgentStore) -> ConversationRecord:
    moment = datetime.now(timezone.utc)
    conversation = ConversationRecord(
        conversation_id="00000000-0000-0000-0000-000000000001",
        owner_user_id="user-1",
        created_at=moment,
        updated_at=moment,
    )
    store.create_conversation(conversation)
    return conversation


def _message(
    store: InMemoryAgentStore,
    *,
    conversation_id: str,
    message_id: str,
    task_id: str,
    content: str,
    status: str = "completed",
    created_at: datetime | None = None,
) -> MessageRecord:
    moment = created_at or datetime.now(timezone.utc)
    message = MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        role="assistant" if message_id.endswith("a") else "user",
        content=content,
        status=status,
        task_id=task_id,
        created_at=moment,
        updated_at=moment,
    )
    store.add_message(message)
    return message


def _task(
    *,
    conversation_id: str,
    task_id: str,
    response_message_id: str | None = None,
) -> TaskRecord:
    moment = datetime.now(timezone.utc)
    return TaskRecord(
        task_id=task_id,
        source_type="chat",
        owner_user_id="user-1",
        status="succeeded",
        input={"text": "当前问题"},
        conversation_id=conversation_id,
        response_message_id=response_message_id,
        created_at=moment,
        updated_at=moment,
    )


def test_context_excludes_summary_boundary_current_task_and_pending_messages() -> None:
    store = InMemoryAgentStore()
    conversation = _conversation(store)
    base = datetime.now(timezone.utc)
    boundary = _message(
        store,
        conversation_id=conversation.conversation_id,
        message_id="00000000-0000-0000-0000-000000000010",
        task_id="old-task",
        content="已经进入摘要",
        created_at=base,
    )
    visible = _message(
        store,
        conversation_id=conversation.conversation_id,
        message_id="00000000-0000-0000-0000-000000000011a",
        task_id="old-task-2",
        content="摘要之后仍需保留",
        created_at=base + timedelta(seconds=1),
    )
    _message(
        store,
        conversation_id=conversation.conversation_id,
        message_id="00000000-0000-0000-0000-000000000012a",
        task_id="current-task",
        content="当前 Task 不应该重复进入上下文",
        created_at=base + timedelta(seconds=2),
    )
    _message(
        store,
        conversation_id=conversation.conversation_id,
        message_id="00000000-0000-0000-0000-000000000013a",
        task_id="old-task-3",
        content="pending 不进入上下文",
        status="pending",
        created_at=base + timedelta(seconds=3),
    )
    conversation.summary = "旧摘要"
    conversation.summary_until_message_id = boundary.message_id
    conversation.summary_version = 1
    store.save_conversation(conversation)

    service = ConversationContextService(
        store,
        make_settings(
            conversation_context_enabled=True,
            conversation_context_recent_max_tokens=4000,
        ),
    )
    context = service.load(_task(
        conversation_id=conversation.conversation_id,
        task_id="current-task",
    ))

    assert context is not None
    assert context.summary == "旧摘要"
    assert [item.message_id for item in context.recent_messages] == [
        visible.message_id
    ]


def test_summary_job_updates_conversation_with_cas() -> None:
    store = InMemoryAgentStore()
    conversation = _conversation(store)
    base = datetime.now(timezone.utc)
    expected_message_ids: list[str] = []
    for index in range(10):
        suffix = f"{index + 100:011d}a"
        message = _message(
            store,
            conversation_id=conversation.conversation_id,
            message_id=f"00000000-0000-0000-0000-{suffix}",
            task_id="task-old",
            content=f"消息 {index}",
            created_at=base + timedelta(seconds=index),
        )
        expected_message_ids.append(message.message_id)
    task = _task(
        conversation_id=conversation.conversation_id,
        task_id="task-old",
        response_message_id=expected_message_ids[-1],
    )
    store.tasks[task.task_id] = task

    class Provider:
        def summarize(self, *, previous_summary, messages, max_tokens):
            assert previous_summary == ""
            assert len(messages) == 10
            assert max_tokens == 600
            return "更新后的摘要"

    service = ConversationSummaryService(
        store,
        make_settings(
            conversation_summary_enabled=True,
            conversation_summary_min_messages=10,
            conversation_summary_target_tokens=600,
        ),
        Provider(),
    )

    job = service.enqueue_after_task(task.task_id)
    assert job is not None
    assert job.expected_summary_version == 0
    assert service.run_once(limit=1) == 1

    updated = store.get_conversation(conversation.conversation_id)
    assert updated is not None
    assert updated.summary == "更新后的摘要"
    assert updated.summary_version == 1
    assert updated.summary_until_message_id == expected_message_ids[-1]


def test_answer_capability_receives_conversation_context() -> None:
    captured: dict[str, object] = {}

    class AnswerProvider:
        def compose(self, question, evidence, *, conversation_context=None):
            captured["context"] = conversation_context
            return type(
                "Draft",
                (),
                {"answer": "ok", "citations": [], "model_calls": 0},
            )()

    from app.capabilities.answer import AnswerComposeCapability

    context = ConversationContext(
        conversation_id="conversation-1",
        summary="同一个会话的摘要",
        summary_until_message_id="message-1",
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
    capability = AnswerComposeCapability(AnswerProvider())
    with bind_execution_context(execution):
        result = capability.execute(
            capability.validate({"question": "继续", "evidence": []})
        )

    assert result["answer"] == "ok"
    assert captured["context"] is context


def test_understanding_receives_bound_conversation_context() -> None:
    store = InMemoryAgentStore()
    captured: dict[str, object] = {}

    class UnderstandingProvider:
        name = "capturing"
        model_backed = False
        estimated_model_calls = 0
        last_call_count = 0

        def understand(
            self,
            task,
            *,
            conversation_context=None,
            min_confidence=None,
        ):
            captured["context"] = conversation_context
            from app.kernel.models import TaskUnderstanding

            return TaskUnderstanding(is_task=True, goal="继续", confidence=1.0)

    runtime = AgentRuntime(
        store=store,
        registry=CapabilityRegistry([]),
        planner=object(),
        policy=object(),
        understanding_provider=UnderstandingProvider(),
        understanding_mode="enforce",
    )
    task = _task(conversation_id="conversation-1", task_id="task-1")
    context = ConversationContext(
        conversation_id="conversation-1",
        summary="bound summary",
    )
    with bind_conversation_context(context):
        runtime._call_understanding_provider(task)

    assert captured["context"] is context
