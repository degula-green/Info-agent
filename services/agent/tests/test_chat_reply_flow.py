"""Chat replies: who gets one, who does not, and what the user ends up seeing.

The collection entry must not start answering group-chat asides, a greeting
must stop being reported as an "unsupported intent", and the reply has to land
in the task result the front end already renders.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.capabilities.chat_reply import ChatReplyCapability
from app.capabilities.todo import TodoCreateCapability
from app.container import build_container
from app.kernel.models import (
    CapabilityDescriptor,
    TaskEnvelope,
    TaskUnderstanding,
    UnderstandingIntent,
)
from app.kernel.registry import CapabilityRegistry
from app.planning.deterministic import DeterministicPlanner
from app.policy.descriptor import DescriptorPolicy
from app.providers.chat import ChatReplyDraft
from app.testing.fake_knowledge import FakeKnowledgeClient
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from app.understanding.provider import FakeUnderstandingProvider
from tests.support import create_task, knowledge_event, make_settings, snapshot


class FakeChatProvider:
    def __init__(self, reply: str = "晚上好呀，需要我做什么随时说") -> None:
        self.text = reply
        self.calls: list[str] = []

    def reply(self, text: str) -> ChatReplyDraft:
        self.calls.append(text)
        return ChatReplyDraft(reply=self.text, model_calls=1)


def understanding(name: str) -> TaskUnderstanding:
    labels = {
        "non_task": False,
        "todo.create": True,
        "other_task": True,
    }
    return TaskUnderstanding(
        is_task=labels.get(name, True),
        goal="probe",
        intent_candidates=[UnderstandingIntent(name=name, confidence=0.95)],
    )


def envelope(text: str, *, source_type: str = "chat") -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-1",
        source_type=source_type,
        owner_user_id="user-1",
        input={"text": text},
        source_ref={},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def reply_descriptor() -> CapabilityDescriptor:
    return ChatReplyCapability(FakeChatProvider()).descriptor


def todo_descriptor() -> CapabilityDescriptor:
    return TodoCreateCapability(InMemoryTodoStore()).descriptor


def test_a_chat_aside_is_planned_as_one_reply_step() -> None:
    planner = DeterministicPlanner(default_timezone="Asia/Shanghai")

    plan = planner.create_plan(
        envelope("晚上好"),
        [todo_descriptor(), reply_descriptor()],
        [],
        understanding=understanding("non_task"),
    )

    assert [step.capability for step in plan.steps] == ["chat.reply"]
    assert plan.steps[0].arguments == {"text": "晚上好"}
    assert plan.unsupported_intents == []


def test_an_aside_is_no_longer_reported_as_an_unsupported_intent() -> None:
    """A greeting is not "an intent we cannot serve"; it is not an intent."""

    planner = DeterministicPlanner(default_timezone="Asia/Shanghai")

    plan = planner.create_plan(
        envelope("晚上好"),
        [todo_descriptor()],
        [],
        understanding=understanding("non_task"),
    )

    assert plan.steps == []
    assert plan.unsupported_intents == []
    assert plan.warnings == []


def test_without_the_capability_an_aside_still_produces_nothing() -> None:
    """The switch is the registry: no capability, no reply, no error."""

    planner = DeterministicPlanner(
        default_timezone="Asia/Shanghai", reply_capability_name="chat.reply"
    )

    plan = planner.create_plan(
        envelope("晚上好"),
        [todo_descriptor()],
        [],
        understanding=understanding("non_task"),
    )

    assert plan.steps == []
    assert plan.warnings == []


def test_a_collected_aside_never_gets_a_reply() -> None:
    """Collection only ever creates to-dos; nothing answers on the user's behalf."""

    planner = DeterministicPlanner(default_timezone="Asia/Shanghai")

    plan = planner.create_plan(
        envelope("今天这个会开得挺久的", source_type="knowledge_event"),
        [todo_descriptor(), reply_descriptor()],
        [],
        understanding=understanding("non_task"),
    )

    assert plan.steps == []
    assert plan.warnings == []


def test_a_task_we_cannot_serve_is_still_reported_as_unsupported() -> None:
    """other_task is a real task; only chit-chat stopped being reported."""

    planner = DeterministicPlanner(default_timezone="Asia/Shanghai")

    plan = planner.create_plan(
        envelope("帮我订一张明天去北京的高铁票"),
        [todo_descriptor(), reply_descriptor()],
        [],
        understanding=understanding("other_task"),
    )

    assert plan.steps == []
    assert plan.unsupported_intents == ["other_task"]
    assert plan.warnings == ["unsupported intent: other_task"]


def build(*, provider=None, replies_enabled: bool = True):
    settings = make_settings(understanding_mode="enforce")
    todo_store = InMemoryTodoStore()
    capabilities = [TodoCreateCapability(todo_store, default_timezone=settings.default_timezone)]
    provider = provider or FakeChatProvider()
    if replies_enabled:
        capabilities.append(ChatReplyCapability(provider))
    registry = CapabilityRegistry(capabilities)
    store = InMemoryAgentStore()
    container = build_container(
        settings=settings,
        store=store,
        todo_store=todo_store,
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=DeterministicPlanner(default_timezone=settings.default_timezone),
        policy=DescriptorPolicy(registry),
        knowledge=FakeKnowledgeClient(snapshots={}),
        understanding_provider=FakeUnderstandingProvider(understanding("non_task")),
    )
    return container, store, provider


def test_the_reply_reaches_the_task_result_the_front_end_renders() -> None:
    container, store, provider = build()
    task = create_task(container, text="晚上好")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert record.result["answer"] == "晚上好呀，需要我做什么随时说"
    assert record.result["warnings"] == []
    assert provider.calls == ["晚上好"]
    assert [item.capability for item in store.list_observations(task.task_id)] == [
        "chat.reply"
    ]
    # The reply spends a model call, and the Task budget is charged for it.
    assert record.model_call_count == 1


def test_an_aside_never_pauses_for_approval() -> None:
    """Nothing is written, so nothing needs approving."""

    container, store, _provider = build()
    task = create_task(container, text="晚上好")

    container.execution_service.run_task(task.task_id)

    assert store.list_approvals(task_id=task.task_id) == []


def test_replies_can_be_switched_off_without_a_code_change() -> None:
    container, store, provider = build(replies_enabled=False)
    task = create_task(container, text="晚上好")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert provider.calls == []
    assert "answer" not in (record.result or {})
    assert record.result["warnings"] == []


def test_a_collected_message_is_still_only_ever_a_to_do() -> None:
    """End to end through the collection entry: no reply, to-do when it is one."""

    settings = make_settings(understanding_mode="enforce")
    todo_store = InMemoryTodoStore()
    provider = FakeChatProvider()
    registry = CapabilityRegistry(
        [
            TodoCreateCapability(
                todo_store, default_timezone=settings.default_timezone
            ),
            ChatReplyCapability(provider),
        ]
    )
    store = InMemoryAgentStore()
    container = build_container(
        settings=settings,
        store=store,
        todo_store=todo_store,
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=DeterministicPlanner(default_timezone=settings.default_timezone),
        policy=DescriptorPolicy(registry),
        knowledge=FakeKnowledgeClient(
            snapshots={"item-1": snapshot(text="明天晚上八点开个评审会")}
        ),
        understanding_provider=FakeUnderstandingProvider(understanding("todo.create")),
    )

    outcome = container.knowledge_events.handle(knowledge_event())

    assert len(outcome.task_ids) == 1
    container.execution_service.run_task(outcome.task_ids[0])
    assert provider.calls == []
    assert "answer" not in (store.get_task(outcome.task_ids[0]).result or {})
