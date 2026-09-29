from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.config import settings


logger = logging.getLogger("rag.stream")


class StreamUnavailable(RuntimeError):
    pass


def validate_envelope(value: dict[str, Any]) -> dict[str, Any]:
    required = ("event_id", "event_type", "schema_version", "occurred_at", "trace_id", "organization_id", "producer", "payload")
    if not isinstance(value, dict) or any(name not in value for name in required):
        raise ValueError("event envelope is missing required fields")
    # Presence is checked above; here only the string fields are checked for a
    # non-empty value. `schema_version` is a number, so folding it into this
    # test rejected a valid `schema_version: 0` as "empty".
    if any(not value.get(name) for name in ("event_id", "event_type", "occurred_at", "trace_id", "producer")):
        raise ValueError("event envelope has empty required fields")
    if not isinstance(value["payload"], dict):
        raise ValueError("event payload must be an object")
    if value["event_type"] == "knowledge.ready":
        payload_fields = (
            "resource_type", "resource_id", "knowledge_item_id",
            "source_audience_policy", "content_version", "acl_version",
            "content_hash", "content_access_required",
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
        self._group_ready = False
        if not self.stream:
            raise StreamUnavailable("RAG_REDIS_INBOUND_STREAM is not configured")

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise StreamUnavailable("could not create Redis consumer group") from exc
        self._group_ready = True

    def _ensure_group_once(self) -> None:
        """Create the consumer group at most once per Redis connection.

        XGROUP CREATE is idempotent but it is still a round trip, and when Redis
        is unreachable it fails *before* anything has been read, so a transient
        connect timeout aborted the whole iteration. The group only has to be
        created when it might not exist yet; `reconnect` clears the flag so a
        fresh connection re-creates it after a Redis restart.
        """
        if getattr(self, "_group_ready", False):
            return
        self.ensure_group()

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
        self._group_ready = False

    def _claim_pending(self) -> list[tuple[str, list]]:
        """Reclaim entries left pending by a crashed or previous consumer.

        XAUTOCLAIM answers with ``[next_start_id, messages, deleted_ids]`` and
        the caller is expected to ask again from ``next_start_id`` until it
        comes back as ``0-0``. Discarding that cursor (as this did) meant every
        iteration re-scanned the head of the PEL: once a permanently failing
        entry sat in the first page it was re-claimed, re-processed and
        re-failed forever, and entries behind it were never reached.

        Still best-effort, so Redis versions without XAUTOCLAIM keep working;
        new entries are read by ``run_once`` regardless.

        Note the coupling with ``redis_claim_idle_ms``: XAUTOCLAIM takes any
        entry idle longer than that threshold, and a MinerU job can legally run
        for ``mineru_task_max_wait_seconds``, far beyond it. An in-flight job is
        therefore reclaimed and processed a second time. That is pre-existing,
        and the handler absorbs it by resuming the existing job rather than
        starting a new one, but the sweep should not make it larger: the round
        cap bounds how many entries one iteration can pull in.
        """
        xautoclaim = getattr(self.client, "xautoclaim", None)
        if not callable(xautoclaim):
            return []
        rows: list[tuple[str, list]] = []
        cursor = "0-0"
        for _ in range(max(1, settings.redis_claim_max_rounds)):
            try:
                claimed = xautoclaim(
                    self.stream,
                    self.group,
                    self.consumer,
                    min_idle_time=settings.redis_claim_idle_ms,
                    start_id=cursor,
                    count=settings.redis_batch_size,
                )
            except Exception as exc:
                logger.warning("XAUTOCLAIM failed; skipping pending sweep: %s", exc)
                break
            # redis-py has returned both tuples and lists for XAUTOCLAIM across
            # supported releases. The wire shape is the same.
            if not isinstance(claimed, (tuple, list)) or len(claimed) < 2:
                break
            messages = claimed[1] or []
            if messages:
                rows.append((self.stream, messages))
            next_cursor = claimed[0]
            next_cursor = next_cursor.decode() if isinstance(next_cursor, bytes) else str(next_cursor or "0-0")
            if next_cursor == "0-0" or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows

    def _delivery_count(self, message_id: Any) -> int:
        """How many times Redis has delivered this entry to the group.

        Read only on the failure path, so the extra round trip costs nothing in
        the normal case. Returns 0 when the client cannot answer, which keeps a
        message pending rather than dead-lettering it on a guess.
        """
        xpending_range = getattr(self.client, "xpending_range", None)
        if not callable(xpending_range):
            return 0
        try:
            entries = xpending_range(self.stream, self.group, min=message_id, max=message_id, count=1)
        except Exception:
            return 0
        for entry in entries or []:
            if isinstance(entry, dict):
                return int(entry.get("times_delivered") or 0)
        return 0

    def _dead_letter(self, message_id: str, fields: Any, exc: BaseException) -> bool:
        """Move an exhausted entry to the DLQ so it stops being redelivered."""
        dlq = settings.redis_dlq_stream_name
        if not dlq:
            return False
        payload = {
            "dlq_source_stream": self.stream,
            "dlq_source_message_id": message_id,
            "dlq_consumer_group": self.group,
            "dlq_error": f"{type(exc).__name__}: {exc}"[:500],
            "dlq_failed_at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry the original entry through untouched so an operator can replay
        # it verbatim after the cause is fixed. Keys are normalized to text
        # (the client is built with decode_responses=False) and `setdefault`
        # keeps a payload field from clobbering the dlq_* metadata.
        if isinstance(fields, dict):
            for key, value in fields.items():
                name = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
                payload.setdefault(name, value)
        try:
            self.client.xadd(dlq, payload, maxlen=settings.redis_dlq_maxlen)
        except Exception as dlq_exc:
            logger.error("could not write message_id=%s to DLQ %s: %s", message_id, dlq, dlq_exc)
            return False
        return True

    def run_once(self) -> int:
        self._ensure_group_once()
        rows = self._claim_pending()
        rows.extend(self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=settings.redis_batch_size, block=settings.redis_block_ms) or [])
        handled = 0
        for _, messages in rows:
            for message_id, fields in messages:
                mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
                # Prepare the entry, separating "this payload is unusable" from
                # "handling it failed". Only the latter may be retried; the
                # former is ACKed and logged because keeping it pending would
                # block the head of the PEL forever.
                try:
                    raw = (fields.get("event") or fields.get(b"event")) if isinstance(fields, dict) else None
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if not raw:
                        logger.warning(
                            "dropping stream entry with no `event` field: stream=%s message_id=%s keys=%s",
                            self.stream, mid, sorted(k for k in fields) if isinstance(fields, dict) else None,
                        )
                        self._ack(message_id, mid)
                        handled += 1
                        continue
                    envelope = validate_envelope(json.loads(raw))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    # ACK means this payload is gone for good, so it must be
                    # visible: an unlogged drop here is indistinguishable from
                    # "the event never arrived".
                    logger.warning(
                        "dropping invalid event envelope: stream=%s message_id=%s error=%s payload=%s",
                        self.stream, mid, exc, str(raw)[:500],
                    )
                    self._ack(message_id, mid)
                    handled += 1
                    continue
                try:
                    self.handler(envelope)
                except Exception as exc:
                    # Never ACK a failed processing task, but stop redelivering
                    # it once the delivery budget is spent: an entry that always
                    # fails otherwise occupies the head of the PEL forever and
                    # starves everything behind it.
                    if self._delivery_count(mid) >= max(1, settings.redis_max_retries) and self._dead_letter(mid, fields, exc):
                        logger.error(
                            "message_id=%s exceeded %s deliveries; moved to DLQ %s",
                            mid, settings.redis_max_retries, settings.redis_dlq_stream_name,
                        )
                        self._ack(message_id, mid)
                        continue
                    logger.warning("handler failed; leaving message_id=%s pending for redelivery: %s", mid, exc)
                    continue
                # Acked outside the handler's scope on purpose: a successful
                # job whose ACK fails must not be mistaken for a failed job and
                # dead-lettered. It stays pending and is redelivered, where the
                # handler's own idempotency check ends it.
                self._ack(message_id, mid)
                handled += 1
        return handled

    def _ack(self, message_id: Any, mid: str) -> None:
        try:
            self.client.xack(self.stream, self.group, message_id)
        except Exception as exc:
            # The entry stays pending and comes back; say so rather than letting
            # a successful job look like it never ran.
            logger.warning("XACK failed for message_id=%s (entry stays pending): %s", mid, exc)

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
