"""Object graph for the Agent service.

The container prefers PostgreSQL and Redis (production) and falls back to the
in-memory store and a no-op publisher when those services are not configured,
which keeps local development and tests runnable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.execution_service import ExecutionService
from app.application.knowledge_events import KnowledgeEventService
from app.application.task_service import TaskService
from app.capabilities.answer import AnswerComposeCapability
from app.capabilities.knowledge import (
    KnowledgeSearchContentCapability,
    KnowledgeSearchSourcesCapability,
)
from app.capabilities.todo import TodoCreateCapability
from app.capabilities.web import WebExtractCapability, WebFetchCapability
from app.config import Settings, settings as default_settings
from app.infrastructure.knowledge.client import HttpKnowledgeClient, KnowledgeClient
from app.infrastructure.core.client import CoreClient, HttpCoreClient, NullCoreClient
from app.infrastructure.rag.client import HttpRAGClient, RAGClient
from app.infrastructure.llm.client import OpenAIChatClient
from app.providers.answer import LlmAnswerProvider
from app.providers.page_fetcher import HttpPageFetcher
from app.ingress.knowledge_events import KnowledgeEventIngress
from app.kernel.models import OutboxEvent
from app.kernel.protocols import AgentStore, TaskEventPublisher, TodoStore
from app.kernel.registry import CapabilityRegistry
from app.planning.deterministic import DeterministicPlanner
from app.policy.descriptor import DescriptorPolicy
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore


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
    todo_store: TodoStore
    understanding_provider: object | None
    task_service: TaskService
    execution_service: ExecutionService
    knowledge_ingress: KnowledgeEventIngress
    knowledge_client: KnowledgeClient
    knowledge_events: KnowledgeEventService
    core_client: CoreClient

    def close(self) -> None:
        # The two stores normally share one pool; closing it twice is a no-op
        # for the first owner and an error for the second, so close once.
        seen: set[int] = set()
        for candidate in (self.store, self.todo_store):
            pool = getattr(candidate, "pool", None)
            if pool is not None and id(pool) not in seen:
                seen.add(id(pool))
                pool.close()


def build_knowledge_client(settings: Settings) -> KnowledgeClient:
    return HttpKnowledgeClient(
        base_url=settings.knowledge_base_url,
        token=settings.knowledge_service_token,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )


def build_core_client(settings: Settings) -> CoreClient:
    if not settings.core_base_url:
        return NullCoreClient()
    return HttpCoreClient(
        base_url=settings.core_base_url,
        timeout_seconds=settings.core_timeout_seconds,
    )


def _timeout_seconds(value: float) -> int:
    """Descriptor timeouts are whole seconds; a sub-second setting still means 1."""

    return max(1, int(round(float(value))))


def build_registry(
    settings: Settings,
    todo_store: TodoStore,
    rag_client: RAGClient | None = None,
) -> CapabilityRegistry:
    """Every capability the Agent can actually execute.

    The capability and the provider it calls are given the same timeout, so the
    descriptor the Runtime checks against and the socket the provider opens
    cannot drift apart. todo.create stays the only writer: the three Step-4
    capabilities are read-only and never ask for approval.
    """

    web_timeout = _timeout_seconds(settings.web_timeout_seconds)
    capabilities = [
        TodoCreateCapability(
            todo_store,
            default_timezone=settings.default_timezone,
        ),
        WebFetchCapability(
            HttpPageFetcher(
                timeout_seconds=settings.web_timeout_seconds,
                max_bytes=settings.web_max_bytes,
                max_redirects=settings.web_max_redirects,
                allow_private_addresses=settings.web_allow_private_addresses,
            ),
            timeout_seconds=web_timeout,
        ),
        WebExtractCapability(timeout_seconds=web_timeout),
        AnswerComposeCapability(
            LlmAnswerProvider(
                OpenAIChatClient(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    timeout_seconds=settings.answer_timeout_seconds,
                    max_output_tokens=settings.answer_max_output_tokens,
                    response_format=settings.llm_response_format,
                )
            ),
            timeout_seconds=_timeout_seconds(settings.answer_timeout_seconds),
        ),
    ]
    if settings.rag_agent_tools_enabled and rag_client is not None:
        capabilities.extend(
            [
                KnowledgeSearchSourcesCapability(rag_client),
                KnowledgeSearchContentCapability(rag_client),
            ]
        )
    return CapabilityRegistry(capabilities)


def build_rag_client(settings: Settings) -> RAGClient:
    return HttpRAGClient(
        base_url=settings.rag_base_url,
        service_token=settings.rag_service_token,
        timeout_seconds=settings.rag_timeout_seconds,
    )


def build_planner(settings: Settings):
    provider = (settings.planner_provider or "deterministic").strip().lower()
    if provider == "deterministic":
        return DeterministicPlanner(
            default_timezone=settings.default_timezone,
            min_confidence=settings.understanding_min_confidence,
        )
    if provider == "llm":
        from app.infrastructure.llm.client import OpenAIChatClient
        from app.planning.llm import OpenAICompatiblePlanner

        return OpenAICompatiblePlanner(
            OpenAIChatClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
                max_output_tokens=settings.llm_max_output_tokens,
                # The Planner is the one caller that passes a real JSON Schema
                # per request; the client is told it may ask for strict
                # decoding here, and to fall back when the provider refuses.
                response_format=settings.llm_planner_response_format,
                json_schema_fallback=settings.llm_json_schema_fallback,
            )
        )
    raise RuntimeError(f"unsupported AGENT_PLANNER_PROVIDER: {provider}")


def understanding_min_confidence(settings: Settings, *, source_type: str = "chat") -> float:
    """Collected messages are noisier than chat, so they carry a higher bar."""

    if source_type == "knowledge_event":
        return float(settings.understanding_min_confidence_collected)
    return float(settings.understanding_min_confidence)


def _build_laya_understanding_provider(settings: Settings):
    from app.infrastructure.laya.client import HttpLayaClient
    from app.understanding.laya import LayaUnderstandingProvider

    return LayaUnderstandingProvider(
        HttpLayaClient(
            base_url=settings.laya_base_url,
            api_key=settings.laya_api_key,
            model=settings.laya_model,
            timeout_seconds=settings.laya_timeout_seconds,
        ),
        min_confidence=settings.laya_min_confidence,
        min_margin=settings.laya_min_margin,
    )


def _build_llm_understanding_provider(settings: Settings):
    from app.infrastructure.llm.client import OpenAIChatClient
    from app.understanding.provider import OpenAICompatibleUnderstandingProvider

    return OpenAICompatibleUnderstandingProvider(
        OpenAIChatClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            response_format=settings.llm_response_format,
        ),
        min_confidence=settings.understanding_min_confidence,
    )


def build_understanding_provider(settings: Settings):
    provider = (settings.understanding_provider or "rules").strip().lower()
    if provider == "fake":
        from app.understanding.provider import FakeUnderstandingProvider

        return FakeUnderstandingProvider()
    if provider == "rules":
        from app.understanding.provider import RuleBasedUnderstandingProvider

        return RuleBasedUnderstandingProvider()
    if provider == "llm":
        return _build_llm_understanding_provider(settings)
    if provider == "laya":
        return _build_laya_understanding_provider(settings)
    if provider == "hybrid":
        from app.understanding.hybrid import HybridUnderstandingProvider

        return HybridUnderstandingProvider(
            laya=_build_laya_understanding_provider(settings),
            fallback=_build_llm_understanding_provider(settings),
        )
    raise RuntimeError(f"unsupported AGENT_UNDERSTANDING_PROVIDER: {provider}")


def build_store(settings: Settings) -> AgentStore:
    if settings.database_url:
        from app.infrastructure.postgres.connection import build_pool
        from app.infrastructure.postgres.store import PostgresAgentStore

        return PostgresAgentStore(build_pool(settings), schema=settings.database_schema)
    return InMemoryAgentStore()


def build_todo_store(settings: Settings) -> TodoStore:
    """The to-do ledger: same database as the runtime, separate tables.

    It is its own store because the two have different lifetimes -- the runtime
    keeps Plans that finish, the desktop keeps unfinished to-dos that do not.
    """

    if settings.database_url:
        from app.infrastructure.postgres.connection import build_pool
        from app.infrastructure.postgres.store import PostgresTodoStore

        return PostgresTodoStore(build_pool(settings), schema=settings.database_schema)
    return InMemoryTodoStore()


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
    todo_store: TodoStore | None = None,
    publisher: TaskEventPublisher | None = None,
    registry: CapabilityRegistry | None = None,
    planner=None,
    policy=None,
    knowledge: KnowledgeClient | None = None,
    core: CoreClient | None = None,
    rag_client: RAGClient | None = None,
    ingress: KnowledgeEventIngress | None = None,
    understanding_provider=None,
) -> AgentContainer:
    resolved = settings or default_settings
    resolved_store = store or build_store(resolved)
    resolved_todo_store = todo_store or build_todo_store(resolved)
    resolved_knowledge = knowledge or build_knowledge_client(resolved)
    resolved_rag_client = rag_client or build_rag_client(resolved)
    registry = registry or build_registry(
        resolved,
        resolved_todo_store,
        resolved_rag_client,
    )
    resolved_planner = planner or build_planner(resolved)
    # The LLM planner re-asks the model when a step's arguments miss the
    # capability schema; it can only do that if it is handed the same
    # validators the deterministic planner uses at plan time.
    if attach := getattr(resolved_planner, "set_validators", None):
        attach(
            {
                name: capability.validate
                for name in [d.name for d in registry.list_descriptors()]
                if (capability := registry.find(name)) is not None
            }
        )
    resolved_policy = policy or DescriptorPolicy(registry)
    resolved_publisher = publisher or build_publisher(resolved)
    resolved_understanding = understanding_provider
    if resolved_understanding is None and resolved.understanding_mode.strip().lower() != "off":
        resolved_understanding = build_understanding_provider(resolved)
    resolved_ingress = ingress or KnowledgeEventIngress(
        platforms=resolved.knowledge_platform_allowlist
    )
    task_service = TaskService(resolved_store)

    return AgentContainer(
        settings=resolved,
        store=resolved_store,
        registry=registry,
        planner=resolved_planner,
        policy=resolved_policy,
        publisher=resolved_publisher,
        todo_store=resolved_todo_store,
        understanding_provider=resolved_understanding,
        task_service=task_service,
        execution_service=ExecutionService(
            store=resolved_store,
            registry=registry,
            planner=resolved_planner,
            policy=resolved_policy,
            publisher=resolved_publisher,
            settings=resolved,
            understanding_provider=resolved_understanding,
        ),
        knowledge_ingress=resolved_ingress,
        knowledge_client=resolved_knowledge,
        knowledge_events=KnowledgeEventService(
            ingress=resolved_ingress,
            knowledge=resolved_knowledge,
            task_service=task_service,
        ),
        core_client=core or build_core_client(resolved),
    )
