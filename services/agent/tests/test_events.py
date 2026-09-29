"""Task Event persistence, ordering and SSE replay tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.kernel.models import TaskEvent
from tests.support import build_test_container, create_task, make_app

USER = {"X-Agent-User-Id": "user-1"}


def test_events_have_unique_sequences_and_support_replay() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(
        container,
        steps=[
            {"capability": "fake.read", "arguments": {"value": "first"}},
            {"capability": "fake.read", "arguments": {"value": "second"}},
        ],
    )
    container.execution_service.run_task(task.task_id)

    events = store.list_events(task.task_id)
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    replayed = store.list_events(task.task_id, after_sequence=2)
    assert [event.sequence for event in replayed] == [value for value in sequences if value > 2]


def test_repeated_subscription_does_not_reexecute_steps() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.read", "arguments": {"value": "x"}}])
    container.execution_service.run_task(task.task_id)

    before = len(store.list_observations(task.task_id))
    for _ in range(3):
        assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert len(store.list_observations(task.task_id)) == before
    assert len({event.event_id for event in store.list_events(task.task_id)}) == len(
        store.list_events(task.task_id)
    )


def test_sse_endpoint_replays_persisted_events() -> None:
    container, _store, _publisher, _registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.read", "arguments": {"value": "x"}}])
    container.execution_service.run_task(task.task_id)
    client = TestClient(make_app(container))

    response = client.get(
        f"/api/agent/v1/tasks/{task.task_id}/events",
        params={"timeout_seconds": 1},
        headers=USER,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: task.accepted" in body
    assert "event: plan.created" in body
    assert "event: step.started" in body
    assert "event: task.completed" in body

    payloads = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    sequences = [item["sequence"] for item in payloads]
    assert sequences == sorted(sequences)
    assert sequences[0] == 1


def test_sse_reconnect_only_sends_missing_events() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.read", "arguments": {"value": "x"}}])
    container.execution_service.run_task(task.task_id)
    client = TestClient(make_app(container))

    response = client.get(
        f"/api/agent/v1/tasks/{task.task_id}/events",
        params={"after": 5, "timeout_seconds": 1},
        headers=USER,
    )
    payloads = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert all(item["sequence"] > 5 for item in payloads)


def test_task_event_model_rejects_negative_sequence() -> None:
    from datetime import datetime, timezone

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskEvent(
            event_id="e1",
            task_id="t1",
            sequence=-1,
            event_type="task.accepted",
            occurred_at=datetime.now(timezone.utc),
        )
