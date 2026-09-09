"""Redis Streams worker entry point.

Run from ``services/rag`` with the same environment as the API:
``python worker.py``. Keeping it separate prevents a web process restart from
silently dropping long-running MinerU jobs.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from app.application.worker import RAGEventHandler
from app.config import settings
from app.infrastructure.events.redis_streams import RedisStreamPublisher, RedisStreamWorker


def main() -> None:
    publisher = RedisStreamPublisher() if settings.redis_outbound_stream else None
    handler = RAGEventHandler(publisher=publisher)
    worker = RedisStreamWorker(handler.handle)
    worker.ensure_group()
    while True:
        handler.flush_outbox()
        worker.run_once()


if __name__ == "__main__":
    main()
