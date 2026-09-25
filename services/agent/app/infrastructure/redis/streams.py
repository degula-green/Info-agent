"""Redis Stream wake-up delivery, outbox dispatch and consumer-group worker.

Redis never carries authoritative Task state: it only delivers a wake-up
signal. The worker reloads the Task from PostgreSQL and resumes from the first
unfinished step.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any, Callable

from app.config import Settings
from app.kernel.models import OutboxEvent
from app.kernel.protocols import AgentStore, TaskEventPublisher

logger = logging.getLogger("agent.streams")


@dataclass(frozen=True)
class StreamMessage:
    message_id: str
    payload: dict[str, Any]


class RedisTaskPublisher(TaskEventPublisher):
    def __init__(self, client: Any, stream: str) -> None:
        self.client = client
        self.stream = stream

    def publish(self, event: OutboxEvent) -> None:
        body = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "task_id": event.task_id,
            "payload": event.payload,
        }
        self.client.xadd(
            self.stream,
            {"event": json.dumps(body, ensure_ascii=False, separators=(",", ":"))},
            maxlen=None,
        )


class OutboxDispatcher:
    """Publishes pending Outbox rows to Redis and records delivery."""

    def __init__(self, store: AgentStore, publisher: TaskEventPublisher, *, batch_size: int = 50) -> None:
        self.store = store
        self.publisher = publisher
        self.batch_size = batch_size

    def dispatch_once(self) -> int:
        published = 0
        for event in self.store.pending_outbox(limit=self.batch_size):
            try:
                self.publisher.publish(event)
            except Exception as exc:  # noqa: BLE001 - delivery is retried from the outbox
                self.store.mark_outbox_failed(event.event_id, f"{type(exc).__name__}: {exc}")
                continue
            self.store.mark_outbox_sent(event.event_id)
            published += 1
        return published


class RedisTaskWorker:
    """At-least-once consumer-group loop over wake-up messages."""

    def __init__(
        self,
        handler: Callable[[str], None],
        *,
        client: Any,
        settings: Settings,
    ) -> None:
        self.handler = handler
        self.client = client
        self.stream = settings.redis_inbound_stream
        self.group = settings.redis_consumer_group
        self.consumer = settings.redis_consumer_name or socket.gethostname()
        self.block_ms = settings.redis_block_ms
        self.batch_size = settings.redis_batch_size
        self.claim_idle_ms = settings.redis_claim_idle_ms

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP means the group already exists
            if "BUSYGROUP" not in str(exc):
                raise

    def _decode(self, message_id: bytes | str, fields: dict) -> StreamMessage | None:
        mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
        raw = fields.get("event") or fields.get(b"event") if isinstance(fields, dict) else None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return StreamMessage(message_id=mid, payload=payload)

    def run_once(self) -> int:
        self.ensure_group()
        rows: list[tuple[Any, Any]] = []
        xautoclaim = getattr(self.client, "xautoclaim", None)
        if callable(xautoclaim):
            try:
                claimed = xautoclaim(
                    self.stream,
                    self.group,
                    self.consumer,
                    min_idle_time=self.claim_idle_ms,
                    start_id="0-0",
                    count=self.batch_size,
                )
                if isinstance(claimed, (tuple, list)) and len(claimed) >= 2 and claimed[1]:
                    rows.append((self.stream, claimed[1]))
            except Exception:  # noqa: BLE001 - reclaim is best effort
                logger.warning("xautoclaim failed", exc_info=True)
        rows.extend(
            self.client.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=self.batch_size,
                block=self.block_ms,
            )
            or []
        )

        handled = 0
        for _, messages in rows:
            for message_id, fields in messages:
                message = self._decode(message_id, fields)
                if message is None:
                    self.client.xack(self.stream, self.group, message_id)
                    continue
                task_id = message.payload.get("task_id")
                if not task_id:
                    self.client.xack(self.stream, self.group, message_id)
                    continue
                try:
                    self.handler(str(task_id))
                except Exception:  # noqa: BLE001 - leave pending for reclaim
                    logger.exception("agent worker failed for task %s", task_id)
                    continue
                self.client.xack(self.stream, self.group, message_id)
                handled += 1
        return handled

    def run_forever(self, *, stop: Callable[[], bool] | None = None) -> None:
        while not (stop and stop()):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the long-lived worker alive
                logger.exception("agent worker iteration failed; retrying")
