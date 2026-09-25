"""Wires the runtime, outbox dispatcher and worker entry point together."""

from __future__ import annotations

from app.config import Settings
from app.kernel.approval import ApprovalGateway
from app.kernel.events import new_outbox_event
from app.kernel.executor import CapabilityExecutor
from app.kernel.limits import ExecutionLimits
from app.kernel.protocols import AgentStore, TaskEventPublisher
from app.kernel.registry import CapabilityRegistry
from app.kernel.runtime import AgentRuntime
from app.infrastructure.redis.streams import OutboxDispatcher


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
    ) -> None:
        self.store = store
        self.settings = settings
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
        result = self.runtime.run_task(task_id)
        return result.status

    def handle_wakeup(self, task_id: str) -> None:
        self.run_task(task_id)

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
