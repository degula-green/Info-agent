"""In-memory publisher and dispatcher helpers for tests."""

from __future__ import annotations

from app.kernel.models import OutboxEvent


class FakeTaskPublisher:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.published: list[OutboxEvent] = []
        self.attempts = 0
        self.fail_times = fail_times

    def publish(self, event: OutboxEvent) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ConnectionError("redis unavailable")
        self.published.append(event.model_copy(deep=True))

    def task_ids(self) -> list[str]:
        return [event.task_id for event in self.published if event.task_id]
