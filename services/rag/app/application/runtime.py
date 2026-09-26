from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.application.callback_service import CallbackLane
from app.application.index_service import IndexStageError, MVPIndexService
from app.application.memory_service import MemoryCandidateService
from app.application.parse_service import MVPParseService, ParseStageError
from app.config import settings
from app.domain.rag import utc_now
from app.infrastructure.events.redis_streams import validate_envelope
from app.infrastructure.persistence.mvp import InMemoryRagMVPRepository, PostgresRagMVPRepository


logger = logging.getLogger("rag.runtime")


class MVPWorkerRuntime:
    def __init__(
        self,
        *,
        repository: object | None = None,
        parse_service: MVPParseService,
        index_service: MVPIndexService,
        memory_service: MemoryCandidateService,
        callback_lane: CallbackLane,
        branch_refresh_service: Any | None = None,
    ) -> None:
        self.repository = repository or (
            PostgresRagMVPRepository() if settings.database_url else InMemoryRagMVPRepository()
        )
        self.parse_service = parse_service
        self.index_service = index_service
        self.memory_service = memory_service
        self.callback_lane = callback_lane
        self.branch_refresh_service = branch_refresh_service
        self.queues = {
            lane: queue.Queue(maxsize=max(1, settings.lane_queue_size))
            for lane in ("parse", "index", "memory")
        }
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def handle(self, envelope: dict[str, Any]) -> None:
        validate_envelope(envelope)
        job = self.repository.create_or_get_job(envelope)
        if job.get("status") not in {"pending", "processing"}:
            return
        self._callback(
            job,
            "processing",
            {"chunk_count": 0, "retryable": True},
        )
        self._enqueue("parse", job["id"])

    def recover(self) -> None:
        for lane in ("parse", "index", "memory"):
            for job in self.repository.claim_jobs(lane, limit=settings.worker_prefetch):
                self._enqueue(lane, job["id"])

    def start(self) -> None:
        self.recover()
        for lane in ("parse", "index", "memory"):
            thread = threading.Thread(
                target=self._lane_loop,
                args=(lane,),
                name=f"rag-{lane}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)
        callback_thread = threading.Thread(
            target=self._callback_loop,
            name="rag-callback",
            daemon=True,
        )
        callback_thread.start()
        self.threads.append(callback_thread)

    def stop(self, *, timeout: float | None = None) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=timeout or settings.worker_shutdown_timeout_seconds)
        self.threads.clear()

    def wait(self) -> None:
        try:
            while not self.stop_event.wait(1.0):
                pass
        except KeyboardInterrupt:
            self.stop()

    def _lane_loop(self, lane: str) -> None:
        while not self.stop_event.is_set():
            try:
                job_id = self.queues[lane].get(timeout=settings.lane_poll_interval_seconds)
            except queue.Empty:
                for job in self.repository.claim_jobs(lane, limit=1):
                    self._enqueue(lane, job["id"])
                continue
            try:
                job = self.repository.get_job(job_id)
                if not job:
                    continue
                if lane == "parse":
                    self._run_parse(job)
                elif lane == "index":
                    self._run_index(job)
                else:
                    self._run_memory(job)
            except Exception as exc:
                logger.exception("lane %s failed job_id=%s", lane, job_id)
            finally:
                self.queues[lane].task_done()

    def _run_parse(self, job: dict[str, Any]) -> None:
        self.repository.update_job(job["id"], status="processing", current_stage="fetch")
        try:
            result = self.parse_service.run(job, _event_payload_from_job(job))
            result.context.raw.setdefault("content_type", result.context.content_type)
            snapshot_id = self.repository.upsert_snapshot(result.context)
            for chunk in result.chunks:
                chunk.resource_snapshot_id = snapshot_id
            self.repository.upsert_chunks(result.chunks)
            if result.chunks:
                self.repository.mark_older_chunks_inactive(
                    knowledge_item_id=result.context.knowledge_item_id,
                    content_version=result.context.content_version,
                )
            self.repository.add_attempt(
                job["id"],
                lane="parse",
                stage="chunk",
                status="succeeded",
                metrics={"chunk_count": len(result.chunks), "status": result.status},
            )
            self.repository.update_job(
                job["id"],
                knowledge_base_id=result.context.knowledge_base_id,
                scope_type=result.context.scope_type,
                scope_id=result.context.scope_id,
                source_conversation_id=result.context.source_conversation_id,
                source_audience_policy=result.context.source_audience_policy,
                current_stage="index",
                status="processing",
                last_error=None,
            )
            self._enqueue("index", job["id"])
        except Exception as exc:
            self._retry_or_fail(job, "parse", exc)

    def _run_index(self, job: dict[str, Any]) -> None:
        self.repository.update_job(job["id"], status="processing", current_stage="index")
        try:
            result = self.index_service.process(job)
            status = result.get("status") or "ready"
            if status == "metadata_only":
                self.repository.add_attempt(
                    job["id"], lane="index", stage="index", status="succeeded",
                    metrics={"status": "metadata_only"},
                )
                self.repository.update_job(
                    job["id"],
                    status="metadata_only",
                    current_stage="callback",
                    finished_at=utc_now(),
                    last_error=None,
                )
                self._callback(job, "metadata_only", {"retryable": False})
                return
            self.repository.add_attempt(
                job["id"], lane="index", stage="index", status="succeeded", metrics=result,
            )
            self.repository.update_job(
                job["id"],
                status="ready",
                current_stage="memory",
                finished_at=utc_now(),
                last_error=None,
            )
            self._callback(job, "ready", {**result, "retryable": False})
            self._enqueue("memory", job["id"])
        except Exception as exc:
            self._retry_or_fail(job, "index", exc)

    def _run_memory(self, job: dict[str, Any]) -> None:
        try:
            result = self.memory_service.process(job)
            self.repository.add_attempt(
                job["id"], lane="memory", stage="memory", status="succeeded", metrics=result,
            )
            self.repository.update_job(job["id"], status="ready", current_stage="callback")
        except Exception as exc:
            self.repository.add_attempt(
                job["id"],
                lane="memory",
                stage="memory",
                status="failed",
                retryable=True,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            logger.warning("memory lane failed job_id=%s: %s", job["id"], exc)
            self.repository.update_job(
                job["id"],
                status="ready",
                current_stage="callback",
                last_error=f"memory: {type(exc).__name__}",
            )

    def _retry_or_fail(self, job: dict[str, Any], lane: str, exc: Exception) -> None:
        code = getattr(exc, "code", type(exc).__name__)
        retryable = bool(getattr(exc, "retryable", True))
        retry_count = int(job.get("retry_count") or 0) + 1
        terminal = not retryable or retry_count >= max(1, settings.task_max_retries)
        self.repository.add_attempt(
            job["id"],
            lane=lane,
            stage=lane,
            status="failed",
            retryable=retryable,
            error_code=code,
            error_message=str(exc),
        )
        if terminal:
            self.repository.update_job(
                job["id"],
                status="failed",
                current_stage="callback",
                retry_count=retry_count,
                finished_at=utc_now(),
                last_error=str(exc)[:2000],
            )
            self._callback(
                job,
                "failed",
                {"error_code": code, "retryable": False},
            )
            return
        self.repository.update_job(
            job["id"],
            status="pending" if lane == "parse" else "processing",
            current_stage="fetch" if lane == "parse" else "index",
            retry_count=retry_count,
            next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            last_error=str(exc)[:2000],
        )

    def _callback(self, job: dict[str, Any], status: str, result: dict[str, Any]) -> None:
        self.repository.add_outbox_event({
            "job_id": job["id"],
            "aggregate_type": "knowledge_item",
            "aggregate_id": job["knowledge_item_id"],
            "event_type": f"knowledge.rag.{status}",
            "event_version": int(job.get("content_version") or 1),
            "schema_version": 1,
            "scope_type": job.get("scope_type") or "organization",
            "scope_id": job.get("scope_id") or "00000000-0000-0000-0000-000000000000",
            "trace_id": job.get("source_event_id"),
            "payload": {
                "knowledge_item_id": job["knowledge_item_id"],
                "source_event_id": job["source_event_id"],
                "rag_job_id": job["id"],
                "content_version": int(job.get("content_version") or 1),
                "acl_version": int(job.get("acl_version") or 0),
                "status": status,
                "error_code": result.get("error_code"),
                "retryable": bool(result.get("retryable", False)),
                "result": {
                    key: value
                    for key, value in result.items()
                    if key not in {"retryable", "error_code"}
                },
                "occurred_at": utc_now(),
            },
        })
        self.callback_lane.flush(limit=10)

    def _enqueue(self, lane: str, job_id: str) -> None:
        try:
            self.queues[lane].put_nowait(job_id)
        except queue.Full:
            logger.info("lane %s queue full; job_id=%s remains recoverable in PostgreSQL", lane, job_id)

    def _callback_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.callback_lane.flush()
            except Exception:
                logger.exception("callback lane flush failed")
            if self.branch_refresh_service is not None:
                try:
                    self.branch_refresh_service.run_pending()
                except Exception:
                    logger.exception("branch refresh lane failed")
            self.stop_event.wait(settings.lane_poll_interval_seconds)


def _event_payload_from_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_type": job["resource_type"],
        "resource_id": job["resource_id"],
        "knowledge_item_id": job["knowledge_item_id"],
        "source_conversation_id": job.get("source_conversation_id"),
        "source_audience_policy": job.get("source_audience_policy"),
        "content_version": int(job["content_version"]),
        "acl_version": int(job.get("acl_version") or 0),
        "content_access_required": False,
    }
