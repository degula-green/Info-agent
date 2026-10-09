"""Executor wiring for the answer-streaming channel."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.container import build_container
from app.kernel.models import CapabilityDescriptor
from app.kernel.registry import CapabilityRegistry
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from tests.support import make_settings


class ValueInput(BaseModel):
    value: str = Field(min_length=1)


class FakeStreamingCapability:
    descriptor = CapabilityDescriptor(
        name="fake.stream",
        description="Stream an answer for executor wiring tests.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.stream_calls = 0
        self.plain_calls = 0

    def validate(self, arguments: dict[str, Any]) -> ValueInput:
        return ValueInput.model_validate(arguments)

    def execute(self, arguments: ValueInput) -> dict[str, Any]:
        self.plain_calls += 1
        return {"answer": "fallback", "citations": [], "model_calls": 0}

    def execute_streaming(
        self, arguments: ValueInput, *, sink, should_cancel=None
    ) -> dict[str, Any]:
        self.stream_calls += 1
        sink.push("hel")
        sink.push("lo")
        sink.complete(
            answer="hello",
            citations=[{"evidence_id": "e1"}],
            warnings=["citation_selection_failed"],
        )
        return {
            "answer": "hello",
            "citations": [{"evidence_id": "e1"}],
            "model_calls": 1,
        }


class FakeSink:
    def __init__(self, **kwargs: Any) -> None:
        self.answer_id = str(kwargs["answer_id"])
        self.step_id = str(kwargs["step_id"])
        self.attempt = int(kwargs["attempt"])
        self.final_seq = 0
        self.offset = 0
        self.answer = ""
        self.citations: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.completed = False
        self.interrupted = False
        self.pushed: list[str] = []

    def push(self, delta: str) -> None:
        self.pushed.append(delta)
        self.final_seq += 1
        self.offset += len(delta.encode("utf-8"))
        self.answer += delta

    def complete(
        self,
        *,
        answer: str,
        citations: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        self.answer = answer
        self.citations = list(citations)
        self.warnings = list(warnings)
        self.completed = True

    def interrupt(self, reason: str) -> None:
        self.interrupted = True
        self.reason = reason


class FakeChannel:
    def __init__(self) -> None:
        self.sinks: list[FakeSink] = []

    def open(self, **kwargs: Any) -> FakeSink:
        sink = FakeSink(**kwargs)
        self.sinks.append(sink)
        return sink


def _container(capability, *, enabled: bool, channel):
    settings = make_settings(answer_streaming_enabled=enabled)
    store = InMemoryAgentStore()
    registry = CapabilityRegistry([capability])
    container = build_container(
        settings=settings,
        store=store,
        todo_store=InMemoryTodoStore(),
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=InputDrivenFakePlanner(),
        answer_stream=channel,
    )
    return container, store


def _run(container, store):
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={
            "text": "go",
            "steps": [
                {"capability": "fake.stream", "arguments": {"value": "x"}}
            ],
        },
    )
    status = container.execution_service.run_task(task.task_id)
    return task.task_id, status


def test_streaming_capability_emits_started_deltas_and_completed() -> None:
    capability = FakeStreamingCapability()
    channel = FakeChannel()
    container, store = _container(capability, enabled=True, channel=channel)

    task_id, status = _run(container, store)

    assert status == "succeeded"
    events = store.list_events(task_id)
    types = [event.event_type for event in events]
    assert "answer.started" in types
    assert "answer.completed" in types
    assert types.index("answer.started") < types.index("answer.completed")
    assert types[-1] == "task.completed"

    sink = channel.sinks[0]
    assert sink.pushed == ["hel", "lo"]
    assert sink.answer == "hello"
    assert sink.final_seq == 2
    assert sink.warnings == ["citation_selection_failed"]
    assert capability.stream_calls == 1
    assert capability.plain_calls == 0

    started = next(event for event in events if event.event_type == "answer.started")
    completed = next(
        event for event in events if event.event_type == "answer.completed"
    )
    assert started.payload["answer_id"] == sink.answer_id
    assert completed.payload["answer_id"] == sink.answer_id
    assert completed.payload["final_seq"] == 2

    observation = store.list_observations(task_id)[0]
    assert observation.output["answer"] == "hello"


def test_streaming_disabled_uses_the_plain_execute_path() -> None:
    capability = FakeStreamingCapability()
    channel = FakeChannel()
    container, store = _container(capability, enabled=False, channel=channel)

    task_id, status = _run(container, store)

    assert status == "succeeded"
    assert capability.stream_calls == 0
    assert capability.plain_calls == 1
    assert channel.sinks == []
    types = [event.event_type for event in store.list_events(task_id)]
    assert "answer.started" not in types


def test_redis_failures_do_not_fail_the_streamed_answer() -> None:
    from app.infrastructure.redis.answer_stream import RedisAnswerStreamSink

    class BrokenStream:
        snapshot_every = 50

        def append_delta(self, **kwargs: Any) -> None:
            raise RuntimeError("redis down")

        def save_snapshot(self, **kwargs: Any) -> None:
            raise RuntimeError("redis down")

        def complete(self, **kwargs: Any) -> None:
            raise RuntimeError("redis down")

        def interrupt(self, **kwargs: Any) -> None:
            raise RuntimeError("redis down")

    sink = RedisAnswerStreamSink(
        BrokenStream(),
        task_id="task-1",
        answer_id="answer-1",
        step_id="step-1",
        attempt=1,
    )

    sink.push("hello")
    sink.complete(answer="hello", citations=[], warnings=[])
    sink.interrupt("boom")

    assert sink.answer == "hello"
    assert sink.final_seq == 1
    assert sink.completed is True
    assert sink.interrupted is True
