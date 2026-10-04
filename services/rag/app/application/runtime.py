from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.application.callback_service import CallbackLane
from app.application.deletion_service import DeletionService
from app.application.index_service import MVPIndexService
from app.application.memory_service import MemoryCandidateService
from app.application.parse_service import MVPParseService
from app.config import settings
from app.domain.rag import utc_now
from app.infrastructure.events.redis_streams import validate_envelope
from app.infrastructure.persistence.mvp import (
    InMemoryRagMVPRepository,
    PostgresRagMVPRepository,
)


logger = logging.getLogger("rag.runtime")


class LeaseLost(RuntimeError):
    pass


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
        deletion_service: DeletionService | None = None,
    ) -> None:
        self.repository = repository or (
            PostgresRagMVPRepository()
            if settings.database_url
            else InMemoryRagMVPRepository()
        )
        self.parse_service = parse_service
        self.index_service = index_service
        self.memory_service = memory_service
        self.callback_lane = callback_lane
        self.branch_refresh_service = branch_refresh_service
        self.deletion_service = deletion_service
        self.queues = {
            lane: queue.Queue(maxsize=max(1, settings.lane_queue_size))
            for lane in ("parse", "index", "memory")
        }
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def handle(self, envelope: dict[str, Any]) -> None:
        validate_envelope(envelope)
        if envelope.get("event_type") == "knowledge.deletion.requested":
            service = self.deletion_service or DeletionService(repository=self.repository, indexer=self.index_service.indexer)
            service.handle(envelope)
            return
        job = self.repository.create_or_get_job(envelope)
        detailed = self.repository.get_job(job["id"])
        if detailed:
            job = detailed
        if job.get("status") not in {
            "pending",
            "processing",
            "retry_wait",
            "ready",
        }:
            return
        self._enqueue("parse", job["id"])

    def recover(self) -> None:
        for lane in ("parse", "index", "memory"):
            for job in self.repository.list_recoverable_jobs(
                lane,
                limit=settings.worker_prefetch,
            ):
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
            job_id: str | None = None
            try:
                job_id = self.queues[lane].get(
                    timeout=settings.lane_poll_interval_seconds
                )
            except queue.Empty:
                claimed = self.repository.claim_jobs(lane, limit=1)
                if claimed:
                    self._process_claimed(lane, claimed[0])
                continue
            try:
                claimed = self.repository.claim_jobs(
                    lane,
                    limit=1,
                    job_id=job_id,
                )
                if claimed:
                    self._process_claimed(lane, claimed[0])
            except Exception:
                logger.exception("lane %s failed job_id=%s", lane, job_id)
            finally:
                self.queues[lane].task_done()

    def _process_claimed(self, lane: str, job: dict[str, Any]) -> None:
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, heartbeat_stop, lease_lost),
            name=f"rag-heartbeat-{lane}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._callback(
                job,
                "processing",
                {"chunk_count": 0, "retryable": True},
            )
            if lane == "parse":
                self._run_parse(job)
            elif lane == "index":
                self._run_index(job)
            else:
                self._run_memory(job)
        except LeaseLost:
            logger.warning(
                "lease lost; stopping lane=%s job_id=%s epoch=%s",
                lane,
                job["id"],
                job.get("lease_epoch"),
            )
        except Exception:
            logger.exception("lane %s failed job_id=%s", lane, job["id"])
        finally:
            heartbeat_stop.set()
            heartbeat.join(
                timeout=max(1.0, float(settings.task_heartbeat_seconds))
            )

    def _heartbeat_loop(
        self,
        job: dict[str, Any],
        stop: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        interval = max(1.0, float(settings.task_heartbeat_seconds))
        while not stop.wait(interval):
            alive = self.repository.heartbeat(
                job["id"],
                owner=str(job["lease_owner"]),
                epoch=int(job["lease_epoch"]),
            )
            if not alive:
                lease_lost.set()
                return

    def _run_parse(self, job: dict[str, Any]) -> None:
        self._owned_update(job, status="processing", current_stage="fetch")
        try:
            result = self.parse_service.run(job, _event_payload_from_job(job))
            result.context.raw.setdefault(
                "content_type",
                result.context.content_type,
            )
            snapshot_id = self.repository.upsert_snapshot(result.context)
            for chunk in result.chunks:
                chunk.resource_snapshot_id = snapshot_id
            self.repository.upsert_chunks(result.chunks)
            if result.chunks:
                self.repository.mark_older_chunks_inactive(
                    knowledge_item_id=result.context.knowledge_item_id,
                    content_version=result.context.content_version,
                )
            self._add_attempt(
                job,
                lane="parse",
                stage="chunk",
                status="succeeded",
                metrics={
                    "chunk_count": len(result.chunks),
                    "status": result.status,
                },
            )
            self._owned_update(
                job,
                knowledge_base_id=result.context.knowledge_base_id,
                scope_type=result.context.scope_type,
                scope_id=result.context.scope_id,
                source_conversation_id=result.context.source_conversation_id,
                source_audience_policy=result.context.source_audience_policy,
                parse_status=(
                    "metadata_only"
                    if result.status == "metadata_only"
                    else "parsed"
                ),
                current_stage="index",
                status="processing",
                lease_owner=None,
                lease_until=datetime.now(timezone.utc),
                last_error=None,
            )
            self._enqueue("index", job["id"])
        except LeaseLost:
            raise
        except Exception as exc:
            self._retry_or_fail(job, "parse", exc)

    def _run_index(self, job: dict[str, Any]) -> None:
        self._owned_update(job, status="processing", current_stage="index")
        try:
            result = self.index_service.process(job)
            status = result.get("status") or "ready"
            if status == "metadata_only":
                self._add_attempt(
                    job,
                    lane="index",
                    stage="index",
                    status="succeeded",
                    metrics={"status": "metadata_only"},
                )
                self._owned_update(
                    job,
                    status="metadata_only",
                    current_stage="callback",
                    lease_owner=None,
                    lease_until=datetime.now(timezone.utc),
                    finished_at=utc_now(),
                    last_error=None,
                )
                self._callback(
                    job,
                    "metadata_only",
                    {"retryable": False},
                )
                return
            self._add_attempt(
                job,
                lane="index",
                stage="index",
                status="succeeded",
                metrics=result,
            )
            self._owned_update(
                job,
                status="ready",
                current_stage="memory",
                lease_owner=None,
                lease_until=datetime.now(timezone.utc),
                finished_at=utc_now(),
                last_error=None,
            )
            self._callback(job, "ready", {**result, "retryable": False})
            self._enqueue("memory", job["id"])
        except LeaseLost:
            raise
        except Exception as exc:
            self._retry_or_fail(job, "index", exc)

    def _run_memory(self, job: dict[str, Any]) -> None:
        try:
            result = self.memory_service.process(job)
            self._add_attempt(
                job,
                lane="memory",
                stage="memory",
                status="succeeded",
                metrics=result,
            )
            self._owned_update(
                job,
                status="ready",
                current_stage="callback",
                lease_owner=None,
                lease_until=datetime.now(timezone.utc),
            )
        except LeaseLost:
            raise
        except Exception as exc:
            self._add_attempt(
                job,
                lane="memory",
                stage="memory",
                status="failed",
                retryable=True,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            logger.warning("memory lane failed job_id=%s: %s", job["id"], exc)
            self._owned_update(
                job,
                status="ready",
                current_stage="callback",
                lease_owner=None,
                lease_until=datetime.now(timezone.utc),
                last_error=f"memory: {type(exc).__name__}",
            )

    def _retry_or_fail(
        self,
        job: dict[str, Any],
        lane: str,
        exc: Exception,
    ) -> None:
        code = getattr(exc, "code", type(exc).__name__)
        retryable = bool(getattr(exc, "retryable", True))
        retry_count = int(job.get("retry_count") or 0) + 1
        terminal = (
            not retryable
            or retry_count >= max(1, settings.task_max_retries)
        )
        self._add_attempt(
            job,
            lane=lane,
            stage=lane,
            status="failed",
            retryable=retryable,
            error_code=code,
            error_message=str(exc),
        )
        if terminal:
            fields: dict[str, Any] = {
                "status": "failed",
                "current_stage": "callback",
                "lease_owner": None,
                "lease_until": datetime.now(timezone.utc),
                "retry_count": retry_count,
                "finished_at": utc_now(),
                "last_error": str(exc)[:2000],
            }
            if lane == "parse":
                fields["parse_status"] = "failed"
            self._owned_update(job, **fields)
            self._callback(
                job,
                "failed",
                {"error_code": code, "retryable": False},
            )
            return
        fields = {
            "status": "retry_wait",
            "current_stage": "fetch" if lane == "parse" else "index",
            "lease_owner": None,
            "lease_until": datetime.now(timezone.utc),
            "retry_count": retry_count,
            "next_retry_at": datetime.now(timezone.utc)
            + timedelta(seconds=5),
            "last_error": str(exc)[:2000],
        }
        if lane == "parse":
            fields["parse_status"] = "failed"
        self._owned_update(job, **fields)

    def _owned_update(self, job: dict[str, Any], **fields: Any) -> None:
        updated = self.repository.update_job_if_owned(
            job["id"],
            owner=str(job["lease_owner"]),
            epoch=int(job["lease_epoch"]),
            fields=fields,
        )
        if not updated:
            raise LeaseLost(f"lease lost for job {job['id']}")

    def _add_attempt(
        self,
        job: dict[str, Any],
        *,
        lane: str,
        stage: str,
        status: str,
        retryable: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        return self.repository.add_attempt(
            job["id"],
            lane=lane,
            stage=stage,
            status=status,
            retryable=retryable,
            error_code=error_code,
            error_message=error_message,
            metrics=metrics,
            lease_owner=str(job["lease_owner"]),
            lease_epoch=int(job["lease_epoch"]),
        )

    def _callback(
        self,
        job: dict[str, Any],
        status: str,
        result: dict[str, Any],
    ) -> None:
        self.repository.add_outbox_event(
            {
                "job_id": job["id"],
                "aggregate_type": "knowledge_item",
                "aggregate_id": job["knowledge_item_id"],
                "event_type": f"knowledge.rag.{status}",
                "event_version": int(job.get("content_version") or 1),
                "schema_version": 1,
                "scope_type": job.get("scope_type") or "organization",
                "scope_id": job.get("scope_id")
                or "00000000-0000-0000-0000-000000000000",
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
            }
        )
        self.callback_lane.flush(limit=10)

    def _enqueue(self, lane: str, job_id: str) -> None:
        try:
            self.queues[lane].put_nowait(job_id)
        except queue.Full:
            logger.info(
                "lane %s queue full; job_id=%s remains recoverable in PostgreSQL",
                lane,
                job_id,
            )

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
    payload = job.get("source_payload")
    if isinstance(payload, dict) and payload:
        return dict(payload)
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
