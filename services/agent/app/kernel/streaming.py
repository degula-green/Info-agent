"""Streaming answer contracts shared by capabilities, the executor and Redis.

The kernel only knows about the sink protocol; the Redis-backed channel is an
infrastructure detail injected at construction time. That keeps the executor
testable with an in-memory sink and keeps a Redis outage from changing kernel
semantics.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol


class AnswerStreamSink(Protocol):
    """One answer attempt's transient output channel."""

    answer_id: str
    step_id: str
    attempt: int
    final_seq: int
    offset: int
    answer: str
    citations: list[dict[str, Any]]
    warnings: list[str]
    completed: bool
    interrupted: bool

    def push(self, delta: str) -> None:
        ...

    def complete(
        self,
        *,
        answer: str,
        citations: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        ...

    def interrupt(self, reason: str) -> None:
        ...


class AnswerStreamChannel(Protocol):
    def open(
        self,
        *,
        task_id: str,
        answer_id: str,
        step_id: str,
        attempt: int,
    ) -> AnswerStreamSink:
        ...


class TaskCancellationProbe:
    """Throttled ``should_cancel`` callback backed by the task store.

    The stream transport calls this from its watcher thread, so a probe failure
    is swallowed by the transport and never kills the stream.
    """

    def __init__(
        self,
        store: Any,
        task_id: str,
        *,
        interval: float = 0.5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._interval = max(0.05, float(interval))
        self._clock = clock or time.monotonic
        self._last_checked = 0.0
        self._cancelled = False

    def __call__(self) -> bool:
        now = self._clock()
        if now - self._last_checked < self._interval:
            return self._cancelled
        self._last_checked = now
        task = self._store.get_task(self._task_id)
        self._cancelled = bool(task is not None and task.status == "cancelled")
        return self._cancelled
