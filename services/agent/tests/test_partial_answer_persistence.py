"""Failed/cancelled streamed text must survive a refresh without entering memory."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.application.task_service import TaskService
from app.container import build_container
from app.kernel.events import new_task_event, utcnow
from app.kernel.models import CapabilityDescriptor
from app.kernel.registry import CapabilityRegistry
from app.kernel.states import EVENT_TASK_CANCELLED
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from tests.support import make_settings


def _container():
    store = InMemoryAgentStore()
    container = build_container(
        settings=make_settings(),
        store=store,
        todo_store=InMemoryTodoStore(),
        publisher=FakeTaskPublisher(),
        registry=CapabilityRegistry([]),
    )
    return container, store


def _new_conversation_task(container):
    return container.task_service.create_task(
        owner_user_id="user-1",
        source_type="chat",
        payload={"text": "hello"},
    )


def test_failed_task_keeps_the_partial_answer_in_the_message() -> None:
    container, store = _container()
    task = _new_conversation_task(container)
    stored = store.get_task(task.task_id)
    assert stored is not None
    stored.status = "failed"
    stored.result = {"partial_answer": "已经流出的半截回答"}
    stored.last_error = {"message": "boom"}
    store.commit(stored)

    container.task_service.sync_task_messages(task.task_id)

    message = store.get_message(stored.response_message_id)
    assert message is not None
    assert message.content == "已经流出的半截回答"
    assert message.status == "failed"
    completed = store.list_completed_messages_after_boundary(
        task.conversation_id or ""
    )
    assert stored.response_message_id not in {
        item.message_id for item in completed
    }


def test_cancelled_task_keeps_the_partial_answer_in_the_message() -> None:
    container, store = _container()
    task = _new_conversation_task(container)
    stored = store.get_task(task.task_id)
    assert stored is not None
    stored.status = "cancelled"
    stored.result = {"partial_answer": "取消前的半截回答"}
    stored.last_error = None
    store.commit(stored)

    container.task_service.sync_task_messages(task.task_id)

    message = store.get_message(stored.response_message_id)
    assert message is not None
    assert message.content == "取消前的半截回答"
    assert message.status == "cancelled"


class ValueInput(BaseModel):
    value: str = Field(min_length=1)


class CancelDuringStreamError(Exception):
    classification = "cancelled"


class CancelDuringStreamCapability:
    """Streams a few deltas, then cancels its own Task mid-answer."""

    descriptor = CapabilityDescriptor(
        name="test.cancel_stream",
        description="Cancel the task while the answer stream is in flight.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self, store: InMemoryAgentStore, task_id: str) -> None:
        self._store = store
        self._task_id = task_id

    def validate(self, arguments: dict[str, Any]) -> ValueInput:
        return ValueInput.model_validate(arguments)

    def execute(self, arguments: ValueInput) -> dict[str, Any]:
        return {"answer": "fallback", "model_calls": 0}

    def execute_streaming(
        self, arguments: ValueInput, *, sink, should_cancel=None
    ) -> dict[str, Any]:
        sink.push("已经流出的")
        sink.push("半截回答")
        task = self._store.get_task(self._task_id)
        assert task is not None
        task.status = "cancelled"
        task.updated_at = utcnow()
        self._store.commit(
            task,
            events=[new_task_event(self._task_id, EVENT_TASK_CANCELLED, {})],
        )
        raise CancelDuringStreamError("owner cancelled")


class Sink:
    def __init__(self, **kwargs: Any) -> None:
        self.answer_id = "a1"
        self.step_id = kwargs["step_id"]
        self.attempt = int(kwargs["attempt"])
        self.final_seq = 0
        self.offset = 0
        self.answer = ""
        self.citations: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.completed = False
        self.interrupted = False

    def push(self, delta: str) -> None:
        self.answer += delta
        self.final_seq += 1

    def complete(self, *, answer, citations, warnings) -> None:
        self.completed = True

    def interrupt(self, reason: str) -> None:
        self.interrupted = True


class Channel:
    def open(self, **kwargs: Any) -> Sink:
        return Sink(**kwargs)


def test_runtime_cancel_keeps_the_partial_answer_in_the_message() -> None:
    store = InMemoryAgentStore()
    task = TaskService(store).create_task(
        owner_user_id="user-1",
        source_type="chat",
        payload={
            "text": "long question",
            "steps": [
                {
                    "capability": "test.cancel_stream",
                    "arguments": {"value": "x"},
                }
            ],
        },
    )
    capability = CancelDuringStreamCapability(store, task.task_id)
    container = build_container(
        settings=make_settings(answer_streaming_enabled=True),
        store=store,
        todo_store=InMemoryTodoStore(),
        publisher=FakeTaskPublisher(),
        registry=CapabilityRegistry([capability]),
        answer_stream=Channel(),
        planner=InputDrivenFakePlanner(),
    )

    status = container.execution_service.run_task(task.task_id)

    assert status == "cancelled"
    stored = store.get_task(task.task_id)
    assert stored is not None
    assert stored.status == "cancelled"
    message = store.get_message(stored.response_message_id)
    assert message is not None
    assert message.content == "已经流出的半截回答"
    assert message.status == "cancelled"
