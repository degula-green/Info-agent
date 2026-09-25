"""knowledge.ready consumer entry point.

Run from ``services/agent`` with the same environment as the API:
``python knowledge_worker.py``. It consumes the Knowledge outbox stream with its
own consumer group, fans the event out to Tasks and ACKs only when the entry is
fully handled; everything else stays pending for ``xautoclaim``.
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from app.config import settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.infrastructure.redis.connection import build_redis  # noqa: E402
from app.infrastructure.redis.knowledge_consumer import RedisKnowledgeEventWorker  # noqa: E402

logger = logging.getLogger("agent.knowledge_worker")

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
    global _stop
    _stop = True


def main() -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    if not settings.redis_url:
        logger.error("AGENT_REDIS_URL is not configured; knowledge consumer cannot start")
        return

    container = build_container(settings)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    def handle(payload) -> bool:
        outcome = container.knowledge_events.handle(payload)
        level = logging.INFO if outcome.ack else logging.WARNING
        logger.log(
            level,
            "knowledge event %s -> %s (%s) tasks=%s reason_codes=%s",
            payload.get("event_id"),
            outcome.action,
            outcome.reason,
            list(outcome.task_ids),
            list(outcome.reason_codes),
        )
        return outcome.ack

    worker = RedisKnowledgeEventWorker(
        handle,
        client=build_redis(settings),
        stream=settings.knowledge_ready_stream,
        group=settings.knowledge_consumer_group,
        consumer=settings.redis_consumer_name,
        block_ms=settings.redis_block_ms,
        batch_size=settings.redis_batch_size,
        claim_idle_ms=settings.redis_claim_idle_ms,
    )

    logger.info(
        "consuming %s as group %s", settings.knowledge_ready_stream, settings.knowledge_consumer_group
    )
    while not _stop:
        try:
            worker.run_once()
        except Exception:  # noqa: BLE001 - keep the long-lived consumer alive
            logger.exception("knowledge consumer iteration failed; retrying")
            time.sleep(1.0)

    container.close()


if __name__ == "__main__":
    main()
