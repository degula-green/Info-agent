from __future__ import annotations

import logging
from typing import Any

from app.application.mvp_ports import CallbackPublisher
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository, PostgresRagMVPRepository
from app.config import settings


logger = logging.getLogger("rag.callback")


class CallbackLane:
    def __init__(self, *, repository: object | None = None, publisher: CallbackPublisher) -> None:
        self.repository = repository or (
            PostgresRagMVPRepository() if settings.database_url else InMemoryRagMVPRepository()
        )
        self.publisher = publisher

    def flush(self, *, limit: int = 50) -> int:
        published = 0
        for event in self.repository.pending_outbox(limit=limit):
            try:
                self.publisher.send(event.get("payload") or {})
                self.repository.mark_outbox_published(event["event_id"])
                published += 1
            except Exception as exc:
                logger.warning("callback failed event_id=%s: %s", event["event_id"], exc)
                self.repository.mark_outbox_failed(event["event_id"], type(exc).__name__)
        return published
