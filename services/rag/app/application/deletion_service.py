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
    def __init__(self, *, repository: ChunkRepository, indexer: Any) -> None:
        self.repository = repository
        self.indexer = indexer

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
        logger.info(
            "resource deletion completed knowledge_item_id=%s resource_id=%s vectors=%s chunks=%s",
            knowledge_item_id,
            resource_id,
            deleted_vectors,
            deleted_chunks,
        )
        return result
