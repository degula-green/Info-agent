from __future__ import annotations

import json
import os
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
        self.consumer = settings.redis_consumer_name or f"{socket.gethostname()}-{os.getpid()}"
        if not self.stream:
            raise StreamUnavailable("RAG_REDIS_INBOUND_STREAM is not configured")

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise StreamUnavailable("could not create Redis consumer group") from exc

    def reconnect(self) -> None:
        """Drop stale pooled sockets so the next iteration reconnects cleanly."""
        pool = getattr(self.client, "connection_pool", None)
        disconnect = getattr(pool, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass
        self.client = _build_redis()

    def run_once(self) -> int:
        self.ensure_group()
        rows = []
        # Reclaim entries left pending by a crashed/previous consumer. This is
        # deliberately best-effort so Redis versions without XAUTOCLAIM still
        # work; new entries are read below regardless.
        xautoclaim = getattr(self.client, "xautoclaim", None)
        if callable(xautoclaim):
            try:
                claimed = xautoclaim(
                    self.stream,
                    self.group,
                    self.consumer,
                    min_idle_time=settings.redis_claim_idle_ms,
                    start_id="0-0",
                    count=settings.redis_batch_size,
                )
                # redis-py has returned both tuples and lists for XAUTOCLAIM
                # across supported releases. The wire shape is the same:
                # [next_start_id, messages, deleted_ids].
                if isinstance(claimed, (tuple, list)) and len(claimed) >= 2 and claimed[1]:
                    rows.append((self.stream, claimed[1]))
            except Exception:
                pass
        rows.extend(self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=settings.redis_batch_size, block=settings.redis_block_ms) or [])
        handled = 0
        for _, messages in rows:
            for message_id, fields in messages:
                mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
                try:
                    raw = (fields.get("event") or fields.get(b"event")) if isinstance(fields, dict) else None
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    # Old module-2 internal events used a `payload` field and
                    # are not part of the service-three contract. ACK these
                    # poison entries rather than leaving them pending forever.
                    if not raw:
                        self.client.xack(self.stream, self.group, message_id)
                        handled += 1
                        continue
                    try:
                        envelope = validate_envelope(json.loads(raw))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        self.client.xack(self.stream, self.group, message_id)
                        handled += 1
                        continue
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
    connection_url = settings.redis_url
    if settings.redis_tls and connection_url.startswith("redis://"):
        connection_url = "rediss://" + connection_url[len("redis://"):]
    # XREADGROUP may block for redis_block_ms; the socket timeout must exceed
    # that wait or an idle stream is reported as a transport failure.
    read_timeout = max(
        0.1,
        settings.authz_timeout_seconds,
        getattr(settings, "redis_block_ms", 0) / 1000 + 1,
    )
    return redis.Redis.from_url(
        connection_url,
        db=settings.redis_database,
        username=settings.redis_username or None,
        password=settings.redis_password or None,
        decode_responses=False,
        socket_connect_timeout=max(0.1, getattr(settings, "redis_connect_timeout_seconds", 5.0)),
        socket_timeout=read_timeout,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )
