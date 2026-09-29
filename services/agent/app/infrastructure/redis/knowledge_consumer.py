"""Redis Stream consumer for ``knowledge.ready`` events.

The Knowledge outbox stream is shared: RAG consumes it with ``rag-workers`` and
the Agent consumes the same stream with its own group, so both services see every
message independently. The group is created at ``$`` so a fresh deployment does
not replay the historical backlog.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any, Callable, Mapping

logger = logging.getLogger("agent.knowledge_consumer")


class RedisKnowledgeEventWorker:
    """At-least-once consumer: the handler returns True when the entry is ACKed."""

    def __init__(
        self,
        handler: Callable[[Mapping[str, Any]], bool],
        *,
        client: Any,
        stream: str,
        group: str,
        consumer: str = "",
        block_ms: int = 5000,
        batch_size: int = 10,
        claim_idle_ms: int = 60000,
    ) -> None:
        self.handler = handler
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or socket.gethostname()
        self.block_ms = block_ms
        self.batch_size = batch_size
        self.claim_idle_ms = claim_idle_ms

    def ensure_group(self) -> None:
        try:
            # "$" keeps a new deployment from replaying the whole backlog.
            self.client.xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise

    @staticmethod
    def decode(fields: Any) -> dict[str, Any] | None:
        if not isinstance(fields, dict):
            return None
        raw = fields.get("event")
        if raw is None:
            raw = fields.get(b"event")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

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
                payload = self.decode(fields)
                if payload is None:
                    logger.warning("dropping stream entry without a valid event payload")
                    self.client.xack(self.stream, self.group, message_id)
                    continue
                try:
                    acknowledged = self.handler(payload)
                except Exception:  # noqa: BLE001 - leave pending for reclaim
                    logger.exception("knowledge event handling failed; leaving entry pending")
                    continue
                if not acknowledged:
                    continue
                self.client.xack(self.stream, self.group, message_id)
                handled += 1
        return handled

    def run_forever(self, *, stop: Callable[[], bool] | None = None) -> None:
        while not (stop and stop()):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the long-lived consumer alive
                logger.exception("knowledge consumer iteration failed; retrying")
