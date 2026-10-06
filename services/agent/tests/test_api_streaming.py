"""SSE ordering tests for streamed answers."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.container import build_container
from app.kernel.events import new_task_event
from app.kernel.registry import CapabilityRegistry
from app.kernel.states import (
    EVENT_ANSWER_COMPLETED,
    EVENT_ANSWER_STARTED,
    EVENT_STEP_STARTED,
    EVENT_STEP_SUCCEEDED,
    EVENT_TASK_COMPLETED,
)
from app.testing.fake_capabilities import FakeReadCapability
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from tests.support import make_app, make_settings


class FakeAnswerStream:
    """Reader double matching the RedisAnswerStream surface used by SSE."""

    def __init__(self, deltas: list[dict[str, Any]], *, final_seq: int, text: str):
        self.deltas = deltas
        self.final_seq = final_seq
        self.text = text

    def read_deltas(
        self,
        *,
        task_id: str,
        answer_id: str,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return [item for item in self.deltas if int(item["seq"]) > int(after_seq)]

    def status(self, *, task_id: str, answer_id: str) -> dict[str, Any]:
        return {
            "answer_id": answer_id,
            "next_seq": self.final_seq,
            "final_seq": self.final_seq,
            "done": True,
            "interrupted": False,
            "reason": "",
        }

    def snapshot(self, *, task_id: str, answer_id: str) -> dict[str, Any]:
        return {
            "answer_id": answer_id,
            "text": self.text,
            "next_seq": self.final_seq,
            "completed": True,
            "final_seq": self.final_seq,
            "interrupted": False,
            "citations": [{"evidence_id": "e1"}],
            "warnings": [],
        }

    def open(self, **kwargs: Any):  # pragma: no cover - not used by these tests
        raise AssertionError("executor channel is not used in SSE tests")


def _container(answer_stream: FakeAnswerStream):
    settings = make_settings(answer_streaming_enabled=True)
    store = InMemoryAgentStore()
    registry = CapabilityRegistry([FakeReadCapability()])
    container = build_container(
        settings=settings,
        store=store,
        todo_store=InMemoryTodoStore(),
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=InputDrivenFakePlanner(),
        answer_stream=answer_stream,
    )
    return container, store


def _seed_task(container, store, *, final_seq: int, deltas: list[dict[str, Any]]):
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "hello"}
    )
    store.append_event(
        new_task_event(
            task.task_id,
            EVENT_STEP_STARTED,
            {"step_id": "step-1", "capability": "answer.compose"},
        )
    )
    store.append_event(
        new_task_event(
            task.task_id,
            EVENT_ANSWER_STARTED,
            {"answer_id": "a1", "step_id": "step-1", "attempt": 1, "next_seq": 1},
        )
    )
    store.append_event(
        new_task_event(
            task.task_id,
            EVENT_STEP_SUCCEEDED,
            {"step_id": "step-1", "capability": "answer.compose"},
        )
    )
    store.append_event(
        new_task_event(
            task.task_id,
            EVENT_ANSWER_COMPLETED,
            {
                "answer_id": "a1",
                "step_id": "step-1",
                "attempt": 1,
                "final_seq": final_seq,
                "citations_count": 1,
                "warnings": [],
            },
        )
    )
    store.append_event(
        new_task_event(
            task.task_id,
            EVENT_TASK_COMPLETED,
            {
                "answer": "hello",
                "citations": [{"evidence_id": "e1"}],
                "warnings": [],
            },
        )
    )
    return task


def _event_types(body: str) -> list[str]:
    types: list[str] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("event: "):
                types.append(line[7:].strip())
    return types


def test_sse_drains_deltas_before_task_completed() -> None:
    answer_stream = FakeAnswerStream(
        [
            {"seq": 1, "offset": 1, "delta": "he", "step_id": "step-1", "attempt": 1},
            {"seq": 2, "offset": 3, "delta": "llo", "step_id": "step-1", "attempt": 1},
        ],
        final_seq=2,
        text="hello",
    )
    container, store = _container(answer_stream)
    task = _seed_task(container, store, final_seq=2, deltas=answer_stream.deltas)
    app = make_app(container)

    with TestClient(app) as client:
        response = client.get(
            f"/api/agent/v1/tasks/{task.task_id}/events",
            params={"after": 0, "answer_id": "a1", "answer_after": 0, "timeout_seconds": 1},
            headers={"Authorization": "Bearer user-1-token"},
        )

    types = _event_types(response.text)
    assert types.count("answer.delta") == 2
    assert "answer.completed" in types
    assert "task.completed" in types
    assert types.index("answer.completed") < types.index("task.completed")
    assert types[-1] == "task.completed"
    assert response.text.index("he") < response.text.index('"llo"')


def test_sse_waits_for_a_missing_delta_before_finishing() -> None:
    """final_seq=3 but only two deltas are available: task.completed is held."""

    answer_stream = FakeAnswerStream(
        [
            {"seq": 1, "offset": 1, "delta": "a", "step_id": "step-1", "attempt": 1},
            {"seq": 2, "offset": 2, "delta": "b", "step_id": "step-1", "attempt": 1},
        ],
        final_seq=3,
        text="abc",
    )
    container, store = _container(answer_stream)
    task = _seed_task(container, store, final_seq=3, deltas=answer_stream.deltas)
    app = make_app(container)

    with TestClient(app) as client:
        response = client.get(
            f"/api/agent/v1/tasks/{task.task_id}/events",
            params={"after": 0, "answer_id": "a1", "answer_after": 0, "timeout_seconds": 0.2},
            headers={"Authorization": "Bearer user-1-token"},
        )

    types = _event_types(response.text)
    assert types.count("answer.delta") == 2
    assert "task.completed" not in types
    assert "answer.completed" not in types


def test_answer_snapshot_endpoint_requires_owner_and_returns_text() -> None:
    answer_stream = FakeAnswerStream([], final_seq=0, text="")
    container, store = _container(answer_stream)
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "hello"}
    )
    app = make_app(container)

    with TestClient(app) as client:
        ok = client.get(
            f"/api/agent/v1/tasks/{task.task_id}/answers/a1",
            headers={"Authorization": "Bearer user-1-token"},
        )
        denied = client.get(
            f"/api/agent/v1/tasks/{task.task_id}/answers/a1",
            headers={"Authorization": "Bearer user-2-token"},
        )

    assert ok.status_code == 200
    assert denied.status_code == 403
