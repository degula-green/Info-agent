"""Agent worker entry point.

Run from ``services/agent`` with the same environment as the API:
``python worker.py``. The worker consumes Redis Stream wake-ups, reloads the
Task from PostgreSQL, and resumes from the first unfinished step.
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
from app.infrastructure.redis.streams import RedisTaskWorker  # noqa: E402

logger = logging.getLogger("agent.worker")

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
    global _stop
    _stop = True


def main() -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    container = build_container(settings)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    if not settings.redis_url:
        logger.error("AGENT_REDIS_URL is not configured; worker cannot start")
        return

    worker = RedisTaskWorker(
        container.execution_service.handle_wakeup,
        client=build_redis(settings),
        settings=settings,
    )

    # Re-issue wake-up signals for tasks left unfinished by a previous run.
    recovered = container.execution_service.resume_unfinished_tasks()
    if recovered:
        logger.info("re-queued %s unfinished tasks", recovered)

    while not _stop:
        try:
            container.execution_service.dispatch_outbox()
            worker.run_once()
        except Exception:  # noqa: BLE001 - keep the long-lived worker alive
            logger.exception("agent worker iteration failed; retrying")
            time.sleep(1.0)

    container.close()


if __name__ == "__main__":
    main()
