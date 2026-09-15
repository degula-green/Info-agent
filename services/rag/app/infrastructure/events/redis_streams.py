from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.config import settings


class StreamUnavailable(RuntimeError):
    pass


def validate_envelope(value: dict[str, Any]) -> dict[str, Any]:
    required = ("event_id", "event_type", "schema_version", "occurred_at", "trace_id", "organization_id", "producer", "payload")
    if not isinstance(value, dict) or any(name not in value for name in required):
        raise ValueError("event envelope is missing required fields")
    if any(not value.get(name) for name in ("event_id", "event_type", "schema_version", "occurred_at", "trace_id", "producer")):
        raise ValueError("event envelope has empty required fields")
    if not isinstance(value["payload"], dict):
        raise ValueError("event payload must be an object")
    if value["event_type"] == "knowledge.ready":
        payload_fields = (
            "resource_type", "resource_id", "knowledge_item_id",
            "content_version", "acl_version", "content_variant",
            "content_access_required",
        )
        if any(name not in value["payload"] for name in payload_fields):
            raise ValueError("knowledge.ready payload is missing required fields")
    return value


class RedisStreamPublisher:
    def __init__(self, *, client: Any | None = None, stream: str | None = None) -> None:
        self.client = client or _build_redis()
        self.stream = stream or settings.redis_outbound_stream
        if not self.stream:
            raise StreamUnavailable("RAG_REDIS_OUTBOUND_STREAM is not configured")

    def publish(self, envelope: dict[str, Any]) -> str:
        validate_envelope(envelope)
        fields = {"event": json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))}
        value = self.client.xadd(self.stream, fields, maxlen=None)
        return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class StreamMessage:
    message_id: str
    envelope: dict[str, Any]


class RedisStreamWorker:
    """At-least-once consumer-group loop; handlers decide retry/DLQ policy."""

    def __init__(self, handler: Callable[[dict[str, Any]], None], *, client: Any | None = None) -> None:
        self.client = client or _build_redis()
        self.handler = handler
        self.stream = settings.redis_inbound_stream
        self.group = settings.redis_consumer_group
        self.consumer = settings.redis_consumer_name or socket.gethostname()
        if not self.stream:
            raise StreamUnavailable("RAG_REDIS_INBOUND_STREAM is not configured")

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise StreamUnavailable("could not create Redis consumer group") from exc

    def run_once(self) -> int:
        self.ensure_group()
        rows = self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=settings.redis_batch_size, block=settings.redis_block_ms)
        handled = 0
        for _, messages in rows or []:
            for message_id, fields in messages:
                mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
                try:
                    raw = (fields.get("event") or fields.get(b"event")) if isinstance(fields, dict) else None
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    envelope = validate_envelope(json.loads(raw or "{}"))
                    self.handler(envelope)
                    self.client.xack(self.stream, self.group, message_id)
                    handled += 1
                except Exception:
                    # Leave the pending entry for a later reclaim/DLQ worker;
                    # never ACK a failed processing task.
                    continue
        return handled

    def run_forever(self, *, stop: Callable[[], bool] | None = None) -> None:
        while not (stop and stop()):
            self.run_once()


def _build_redis() -> Any:
    if not settings.redis_url:
        raise StreamUnavailable("RAG_REDIS_URL is not configured")
    try:
        import redis
    except ImportError as exc:
        raise StreamUnavailable("redis package is required for Streams") from exc
    return redis.Redis.from_url(
        settings.redis_url,
        db=settings.redis_database,
        username=settings.redis_username or None,
        password=settings.redis_password or None,
        ssl=settings.redis_tls,
        decode_responses=False,
        socket_connect_timeout=max(0.1, settings.authz_connect_timeout_seconds),
        socket_timeout=max(0.1, settings.authz_timeout_seconds),
    )
