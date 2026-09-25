"""Object graph for the Agent service.

The container prefers PostgreSQL and Redis (production) and falls back to the
in-memory store and a no-op publisher when those services are not configured,
which keeps local development and tests runnable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.execution_service import ExecutionService
from app.application.task_service import TaskService
from app.config import Settings, settings as default_settings
from app.ingress.knowledge_events import KnowledgeEventIngress
from app.kernel.models import OutboxEvent
from app.kernel.protocols import AgentStore, TaskEventPublisher
from app.kernel.registry import CapabilityRegistry
from app.testing.fake_capabilities import FakeReadCapability, FakeWriteCapability
from app.testing.fake_planner import InputDrivenFakePlanner
from app.testing.fake_policy import FakePolicy
from app.testing.in_memory_runtime_store import InMemoryAgentStore


class NullPublisher(TaskEventPublisher):
    """Used when Redis is not configured; the outbox keeps the signal durable."""

    def publish(self, event: OutboxEvent) -> None:  # noqa: ARG002 - intentionally discarded
        return None


@dataclass
class AgentContainer:
    settings: Settings
    store: AgentStore
    registry: CapabilityRegistry
    planner: object
    policy: object
    publisher: TaskEventPublisher
    task_service: TaskService
    execution_service: ExecutionService
    knowledge_ingress: KnowledgeEventIngress

    def close(self) -> None:
        pool = getattr(self.store, "pool", None)
        if pool is not None:
            pool.close()


def build_registry() -> CapabilityRegistry:
    return CapabilityRegistry([FakeReadCapability(), FakeWriteCapability()])


def build_store(settings: Settings) -> AgentStore:
    if settings.database_url:
        from app.infrastructure.postgres.connection import build_pool
        from app.infrastructure.postgres.store import PostgresAgentStore

        return PostgresAgentStore(build_pool(settings), schema=settings.database_schema)
    return InMemoryAgentStore()


def build_publisher(settings: Settings) -> TaskEventPublisher:
    if settings.redis_url:
        from app.infrastructure.redis.connection import build_redis
        from app.infrastructure.redis.streams import RedisTaskPublisher

        return RedisTaskPublisher(build_redis(settings), settings.redis_inbound_stream)
    return NullPublisher()


def build_container(
    settings: Settings | None = None,
    *,
    store: AgentStore | None = None,
    publisher: TaskEventPublisher | None = None,
    registry: CapabilityRegistry | None = None,
    planner=None,
    policy=None,
) -> AgentContainer:
    resolved = settings or default_settings
    resolved_store = store or build_store(resolved)
    registry = registry or build_registry()
    resolved_planner = planner or InputDrivenFakePlanner()
    resolved_policy = policy or FakePolicy(registry)
    resolved_publisher = publisher or build_publisher(resolved)

    return AgentContainer(
        settings=resolved,
        store=resolved_store,
        registry=registry,
        planner=resolved_planner,
        policy=resolved_policy,
        publisher=resolved_publisher,
        task_service=TaskService(resolved_store),
        execution_service=ExecutionService(
            store=resolved_store,
            registry=registry,
            planner=resolved_planner,
            policy=resolved_policy,
            publisher=resolved_publisher,
            settings=resolved,
        ),
        knowledge_ingress=KnowledgeEventIngress(platforms=resolved.knowledge_platform_allowlist),
    )
