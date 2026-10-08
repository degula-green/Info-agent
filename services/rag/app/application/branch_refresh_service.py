from __future__ import annotations

import logging
from typing import Any

from app.application.entity_service import EntityMatcher, match_chunk_mounts


logger = logging.getLogger("rag.branch-refresh")


class BranchRefreshService:
    def __init__(self, *, repository: object, indexer: object) -> None:
        self.repository = repository
        self.indexer = indexer

    def run_pending(self, *, limit: int = 10) -> int:
        processed_jobs = 0
        for job in self.repository.list_pending_branch_refresh_jobs(limit=limit):
            try:
                processed = self._refresh(job)
                self.repository.mark_branch_refresh(
                    job["id"], status="succeeded", processed_count=processed,
                )
                processed_jobs += 1
            except Exception as exc:
                logger.exception("branch refresh failed job_id=%s", job["id"])
                self.repository.mark_branch_refresh(
                    job["id"],
                    status="failed",
                    processed_count=0,
                    error=type(exc).__name__,
                )
        return processed_jobs

    def _refresh(self, job: dict[str, Any]) -> int:
        entities, aliases, version = self.repository.load_entity_registry(
            scope_type=job["scope_type"],
            scope_id=job["scope_id"],
        )
        matcher = EntityMatcher(entities, aliases, registry_version=version)
        chunks = self.repository.list_chunks(
            scope_type=job["scope_type"],
            scope_id=job["scope_id"],
        )
        changed: list[Any] = []
        for chunk in chunks:
            if chunk.lifecycle_status != "active":
                continue
            mounts = match_chunk_mounts(chunk, matcher)
            entity_ids = tuple(mount.entity_id for mount in mounts)
            if entity_ids == chunk.entity_ids:
                # A chunk with no mounts has nothing to persist: registry_version
                # only lives on mount rows, so re-processing every mountless
                # chunk on each registry change would rewrite the whole corpus
                # for no effect.
                if not entity_ids or chunk.registry_version == version:
                    continue
            chunk.entity_ids = entity_ids
            chunk.entity_mounts = tuple(mount.es_document() for mount in mounts)
            chunk.registry_version = version
            self.repository.replace_chunk_mounts(chunk, mounts)
            changed.append(chunk)
        update = getattr(self.indexer, "update_chunk_mounts", None)
        if callable(update):
            update(changed)
        return len(changed)
