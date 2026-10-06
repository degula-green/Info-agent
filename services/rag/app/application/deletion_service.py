from __future__ import annotations

import logging
from typing import Any

from app.application.mvp_ports import ChunkRepository


logger = logging.getLogger("rag.deletion")


class DeletionStageError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class DeletionService:
    def __init__(self, *, repository: ChunkRepository, indexer: Any, callback_lane: Any | None = None) -> None:
        self.repository = repository
        self.indexer = indexer
        self.callback_lane = callback_lane

    def handle(self, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload") or {}
        knowledge_item_id = str(payload.get("knowledge_item_id") or "").strip()
        resource_id = str(payload.get("resource_id") or "").strip()
        if not knowledge_item_id or not resource_id:
            raise DeletionStageError(
                "INVALID_DELETION_EVENT",
                "knowledge_item_id and resource_id are required",
                retryable=False,
            )
        deleted_vectors = self.indexer.delete_resource(resource_id=resource_id)
        deleted_chunks = self.repository.delete_resource_data(
            knowledge_item_id=knowledge_item_id,
            resource_id=resource_id,
        )
        result = {
            "status": "deleted",
            "knowledge_item_id": knowledge_item_id,
            "resource_id": resource_id,
            "deleted_vectors": int(deleted_vectors),
            "deleted_chunks": int(deleted_chunks),
        }
        if self.callback_lane is not None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            self.callback_lane.repository.add_outbox_event(
                {
                    "aggregate_type": "knowledge_item",
                    "aggregate_id": knowledge_item_id,
                    "event_type": "knowledge.rag.deleted",
                    "event_version": int(payload.get("content_version") or 1),
                    "schema_version": 1,
                    "scope_type": str(payload.get("scope_type") or "organization"),
                    "scope_id": str(payload.get("scope_id") or "00000000-0000-0000-0000-000000000000"),
                    "trace_id": str(payload.get("deletion_request_id") or envelope.get("event_id") or ""),
                    "payload": {
                        "knowledge_item_id": knowledge_item_id,
                        "source_event_id": str(payload.get("deletion_request_id") or envelope.get("event_id") or ""),
                        "rag_job_id": str(payload.get("deletion_target_id") or ""),
                        "content_version": int(payload.get("content_version") or 1),
                        "acl_version": int(payload.get("acl_version") or 0),
                        "status": "deleted",
                        "occurred_at": now,
                        "result": result,
                    },
                }
            )
            self.callback_lane.flush(limit=10)
        logger.info(
            "resource deletion completed knowledge_item_id=%s resource_id=%s vectors=%s chunks=%s",
            knowledge_item_id,
            resource_id,
            deleted_vectors,
            deleted_chunks,
        )
        return result
