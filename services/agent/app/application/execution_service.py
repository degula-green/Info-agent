"""Wires the runtime, outbox dispatcher and worker entry point together."""

from __future__ import annotations

import os
import logging
import socket

from app.config import Settings
from app.kernel.errors import TaskNotFoundError
from app.kernel.approval import ApprovalGateway
from app.kernel.events import new_outbox_event
from app.kernel.executor import CapabilityExecutor
from app.kernel.limits import ExecutionLimits
from app.kernel.protocols import AgentStore, TaskEventPublisher
from app.kernel.registry import CapabilityRegistry
from app.kernel.runtime import AgentRuntime
from app.infrastructure.redis.streams import OutboxDispatcher

logger = logging.getLogger("agent.execution")


class ExecutionService:
    def __init__(
        self,
        *,
        store: AgentStore,
        registry: CapabilityRegistry,
        planner,
        policy,
        publisher: TaskEventPublisher,
        settings: Settings,
        lease_owner: str | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        # The lease must identify this driver, not a fixed role name: two drivers
        # sharing "worker" would both pass the lease check and drive the same Task
        # concurrently (the API's /run racing the worker process).
        self.lease_owner = lease_owner or f"{socket.gethostname()}-{os.getpid()}"
        limits = ExecutionLimits.from_settings(settings)
        self.approval_gateway = ApprovalGateway(
            store, expires_seconds=limits.approval_expires_seconds
        )
        self.runtime = AgentRuntime(
            store=store,
            registry=registry,
            planner=planner,
            policy=policy,
            executor=CapabilityExecutor(registry, store),
            approval_gateway=self.approval_gateway,
            limits=limits,
        )
        self.dispatcher = OutboxDispatcher(
            store, publisher, batch_size=settings.outbox_batch_size
        )

    def run_task(self, task_id: str) -> str:
        result = self.runtime.run_task(task_id, lease_owner=self.lease_owner)
        return result.status

    def handle_wakeup(self, task_id: str) -> None:
        try:
            self.run_task(task_id)
        except TaskNotFoundError:
            # A wake-up for a Task that no longer exists (a delete or a stale
            # signal) must be acknowledged instead of retried forever.
            logger.info("dropping wake-up for unknown task %s", task_id)

    def dispatch_outbox(self) -> int:
        return self.dispatcher.dispatch_once()

    def resume_unfinished_tasks(self, limit: int = 50) -> int:
        """Re-issue wake-up signals after a Redis loss or worker restart."""

        recovered = 0
        for task in self.store.list_unfinished_tasks(limit=limit):
            self.store.enqueue_outbox(
                new_outbox_event(task.task_id, "agent.task.wakeup", {"reason": "recovery"})
            )
            recovered += 1
        return recovered
