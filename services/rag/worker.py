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


def main() -> None:
    publisher = RedisStreamPublisher() if settings.redis_outbound_stream else None
    handler = RAGEventHandler(publisher=publisher)
    worker = RedisStreamWorker(handler.handle)
    while True:
        try:
            worker.ensure_group()
            handler.flush_outbox()
            worker.run_once()
        except Exception:
            # Redis providers and intermediate network devices may close an
            # idle blocking read. Keep the long-lived worker alive and let the
            # next iteration reconnect instead of losing new ready events.
            logger.exception("RAG worker iteration failed; retrying")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
