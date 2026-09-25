"""Redis Streams worker entry point.

Run from ``services/rag`` with the same environment as the API:
``python worker.py``. Keeping it separate prevents a web process restart from
silently dropping long-running MinerU jobs.
"""

from pathlib import Path
import logging
import time

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from app.application.worker import RAGEventHandler
from app.config import settings
from app.infrastructure.events.redis_streams import RedisStreamPublisher, RedisStreamWorker


logger = logging.getLogger("rag.worker")


def _configure_logging() -> None:
    """Give the worker a real log configuration.

    Nothing in this service calls ``basicConfig``, and the API only ever logs
    through uvicorn's own config. The worker has no uvicorn, so it fell back to
    ``logging.lastResort``: everything below WARNING was discarded and only
    errors reached stderr. That is precisely the wrong half of the story for a
    consumer that drops, skips and retries work.
    """
    logging.basicConfig(
        level=str(settings.log_level or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # `basicConfig` sets the level on the root logger, so every library that
    # logs through its own logger inherits it. `elastic_transport` logs one INFO
    # line per HTTP request, and indexing a document is three of them: measured
    # over the first minutes after a restart it was 164 of 165 lines (99%), which
    # buries exactly the drop/skip/retry signals this logging was added to carry.
    # Its warnings and errors still surface.
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)


def main() -> None:
    _configure_logging()
    logger.info(
        "rag worker starting: stream=%s group=%s consumer=%s dlq=%s",
        settings.redis_inbound_stream, settings.redis_consumer_group,
        settings.redis_consumer_name or "(generated)", settings.redis_dlq_stream_name or "(disabled)",
    )
    publisher = RedisStreamPublisher() if settings.redis_outbound_stream else None
    handler = RAGEventHandler(publisher=publisher)
    worker = RedisStreamWorker(handler.handle)
    failure_streak = 0
    while True:
        # The outbox flush is the only path that reports job outcomes back to
        # Knowledge, and it does not touch the inbound Redis stream. Running it
        # inside the same try as `run_once` meant a Redis connect timeout
        # aborted the flush before it ran, so an unreachable Redis silently
        # stalled PG -> Knowledge callback delivery as well. Flush first, and
        # flush in its own scope.
        try:
            handler.flush_outbox()
        except Exception:
            logger.exception("RAG worker outbox flush failed; retrying next iteration")
        # `run_once` creates the consumer group itself, at most once per
        # connection (`RedisStreamWorker._ensure_group_once`). Calling
        # `ensure_group()` here as well made every iteration pay an extra round
        # trip that could fail before anything was read.
        try:
            worker.run_once()
            failure_streak = 0
        except Exception:
            # Redis providers and intermediate network devices may close an
            # idle blocking read. Keep the long-lived worker alive and let the
            # next iteration reconnect instead of losing new ready events.
            logger.exception("RAG worker iteration failed; retrying")
            failure_streak = min(failure_streak + 1, 6)
            reconnect = getattr(worker, "reconnect", None)
            if callable(reconnect):
                try:
                    reconnect()
                except Exception:
                    logger.exception("RAG worker Redis reconnect failed")
            time.sleep(min(30.0, 2.0 ** failure_streak))


if __name__ == "__main__":
    main()
