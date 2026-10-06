"""End-to-end routing boundaries for web.research.

The capability tests prove what research does once it runs; these prove when
it runs at all. Both entry points matter: a collected message that happens to
contain a link is still a to-do, and a chat turn that only needs evidence must
not be forced through an answer step.
"""

from __future__ import annotations

from app.capabilities.answer import AnswerComposeCapability
from app.capabilities.todo import TodoCreateCapability
from app.capabilities.web_research import (
    WebResearchCapability,
    WebResearchUrlUnreadable,
)
from app.container import build_container
from app.infrastructure.web.content_reader import ContentReader
from app.kernel.models import (
    Plan,
    PlannerDecision,
    PolicyDecision,
    PlanStep,
    TaskUnderstanding,
    UnderstandingIntent,
)
from app.kernel.registry import CapabilityRegistry
from app.planning.deterministic import DeterministicPlanner
from app.planning.routing import RoutingPlanner
from app.policy.descriptor import DescriptorPolicy
from app.testing.fake_knowledge import FakeKnowledgeClient
from app.testing.fake_providers import FakeAnswerProvider, FakePageFetcher
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from app.understanding.provider import FakeUnderstandingProvider
from tests.support import create_task, knowledge_event, make_settings, snapshot

URL = "https://93.184.216.34/page"


class RecordingResearch(WebResearchCapability):
    """Records that it ran; the reading itself is covered elsewhere."""

    def __init__(self) -> None:
        super().__init__(
            ContentReader(FakePageFetcher({}), renderer=None, min_text_chars=1),
            search_provider=None,
            aliases={},
        )
        self.requests: list[str] = []

    def execute(self, arguments):  # type: ignore[override]
        self.requests.append(arguments.request)
        return {
            "request": arguments.request,
            "urls": list(arguments.urls),
            "queries": list(arguments.queries),
            "include_domains": [],
            "evidence": [
                {
                    "evidence_id": "ev-flow",
                    "source_type": "web",
                    "title": "示例页面",
                    "url": URL,
                    "quote": "正文",
                    "text": "正文",
                    "content_hash": "sha256:0",
                    "retrieved_at": "2026-01-01T00:00:00Z",
                    "fetch_method": "static",
                }
            ],
            "warnings": [],
        }


class UnreadableResearch(RecordingResearch):
    """Raises the same typed failure as a login-walled user URL."""

    def execute(self, arguments):  # type: ignore[override]
        raise WebResearchUrlUnreadable(
            "无法读取用户给出的网址，可能因为页面需要登录、依赖 JavaScript "
            f"或有人机验证：{URL}: 没有抽出可用正文",
            code="explicit_url_unreadable",
        )


class EmptyResearch(RecordingResearch):
    """Returns no evidence so the no-result branch is exercised."""

    def execute(self, arguments):  # type: ignore[override]
        self.requests.append(arguments.request)
        return {
            "request": arguments.request,
            "urls": list(arguments.urls),
            "queries": list(arguments.queries),
            "include_domains": [],
            "evidence": [],
            "warnings": ["no_results"],
        }


class ScriptedWebPlanner:
    """The Planner stand-in the router sends web.research turns to."""

    name = "scripted-web"
    plan_id = "plan-web"

    def __init__(self, *, compose: bool) -> None:
        self.compose = compose
        self.validators: dict | None = None
        self.plans = 0

    def set_validators(self, validators: dict) -> None:
        self.validators = dict(validators)

    def create_plan(self, task, capabilities, observations, constraints=None, understanding=None):
        self.plans += 1
        steps = [
            PlanStep(
                step_id=f"{self.plan_id}-step-1",
                plan_id=self.plan_id,
                order=1,
                capability="web.research",
                arguments={"request": str(task.input.get("text") or ""), "urls": []},
            )
        ]
        if self.compose:
            steps.append(
                PlanStep(
                    step_id=f"{self.plan_id}-step-2",
                    plan_id=self.plan_id,
                    order=2,
                    capability="answer.compose",
                    arguments={
                        "question": "这个页面讲了什么",
                        "evidence": f"$steps.{self.plan_id}-step-1.output.evidence",
                    },
                )
            )
        return Plan(
            plan_id=self.plan_id, task_id=task.task_id, objective="联网研究", steps=steps
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        if any(
            step.status in {"pending", "ready", "running"} for step in current_plan.steps
        ):
            return PlannerDecision(action="continue")
        return PlannerDecision(action="complete")


class AllowAllPolicy:
    """Skips the approval pause: these tests are about routing, not policy."""

    def evaluate(self, task, step, capability=None) -> PolicyDecision:
        return PolicyDecision(action="allow")


def understanding(*names: str, is_task: bool = True) -> TaskUnderstanding:
    return TaskUnderstanding(
        is_task=is_task,
        goal="测试",
        intent_candidates=[UnderstandingIntent(name=name, confidence=0.95) for name in names],
    )


def build(
    *,
    web_intent: str,
    planner_understanding=None,
    llm=None,
    snapshots=None,
    allow_side_effects: bool = False,
    research=None,
    answer_provider=None,
):
    # Understanding is opt-in at the Runtime level, so the routing tests have to
    # turn it on explicitly.
    settings = make_settings(understanding_mode="enforce")
    todo_store = InMemoryTodoStore()
    research = research or RecordingResearch()
    registry = CapabilityRegistry(
        [
            TodoCreateCapability(todo_store, default_timezone=settings.default_timezone),
            research,
            AnswerComposeCapability(
                answer_provider or FakeAnswerProvider("证据整理的回答")
            ),
        ]
    )
    store = InMemoryAgentStore()
    container = build_container(
        settings=settings,
        store=store,
        todo_store=todo_store,
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=RoutingPlanner(
            deterministic=DeterministicPlanner(
                default_timezone=settings.default_timezone
            ),
            llm=llm or ScriptedWebPlanner(compose=True),
        ),
        policy=AllowAllPolicy() if allow_side_effects else DescriptorPolicy(registry),
        knowledge=FakeKnowledgeClient(snapshots=snapshots or {}),
        understanding_provider=FakeUnderstandingProvider(
            planner_understanding or understanding(web_intent)
        ),
    )
    return container, store, research


def test_a_collected_message_with_a_link_never_becomes_web_research() -> None:
    """Collection only ever creates to-dos, whatever the text contains."""

    text = f"看看 {URL} 这个站点的排期"
    llm = ScriptedWebPlanner(compose=True)
    container, store, research = build(
        web_intent="web.research",
        planner_understanding=understanding("web.research"),
        llm=llm,
        snapshots={"item-1": snapshot(text=text)},
        allow_side_effects=True,
    )

    outcome = container.knowledge_events.handle(knowledge_event())

    assert outcome.task_ids
    task = store.get_task(outcome.task_ids[0])
    assert task.source_type == "knowledge_event"
    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert research.requests == []
    assert llm.plans == 0
    assert store.list_observations(task.task_id) == []


def test_a_collected_message_that_is_a_to_do_still_creates_one() -> None:
    """The fixed pipeline keeps working; research simply is not on it."""

    text = f"明天看看 {URL} 的排期"
    container, store, research = build(
        web_intent="todo.create",
        planner_understanding=understanding("todo.create"),
        snapshots={"item-1": snapshot(text=text)},
        allow_side_effects=True,
    )

    outcome = container.knowledge_events.handle(knowledge_event())
    task = store.get_task(outcome.task_ids[0])

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert research.requests == []
    assert [item.capability for item in store.list_observations(task.task_id)] == [
        "todo.create"
    ]


def test_a_chat_turn_that_only_wants_evidence_skips_the_answer_step() -> None:
    container, store, research = build(
        web_intent="web.research", llm=ScriptedWebPlanner(compose=False)
    )
    task = create_task(container, text=f"读一下 {URL}")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert research.requests == [f"读一下 {URL}"]
    assert "answer" not in record.result
    capabilities = [item.capability for item in store.list_observations(task.task_id)]
    assert capabilities == ["web.research"]


def test_a_chat_turn_that_wants_an_answer_composes_one_on_top_of_evidence() -> None:
    container, store, research = build(web_intent="web.research")
    task = create_task(container, text=f"读一下 {URL} 并总结")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert record.result["answer"] == "证据整理的回答"
    assert [item.capability for item in store.list_observations(task.task_id)] == [
        "web.research",
        "answer.compose",
    ]


def test_an_unreadable_explicit_url_fails_without_search_replan() -> None:
    container, store, _research = build(
        web_intent="web.research",
        research=UnreadableResearch(),
    )
    task = create_task(container, text=f"读一下 {URL} 这个网址在讲什么？")

    assert container.execution_service.run_task(task.task_id) == "failed"
    error = store.get_task(task.task_id).last_error
    assert error["code"] == "explicit_url_unreadable"
    assert URL in error["message"]


def test_an_unanswered_url_answer_is_not_marked_success() -> None:
    provider = FakeAnswerProvider(
        f"提供的 evidence 中未提及网址 {URL} 的相关内容。",
        citations=[],
    )
    # The page could not be read at all, which is the case the quality gate
    # exists for: there is no fetched evidence for the requested URL.
    container, store, _research = build(
        web_intent="web.research",
        research=EmptyResearch(),
        answer_provider=provider,
    )
    task = create_task(container, text=f"{URL} 这个网址在讲什么？")

    # The page could not be read, so the honest verdict is that the requested
    # URL was not answered; it must not be reported as a success.
    assert container.execution_service.run_task(task.task_id) == "failed"
    error = store.get_task(task.task_id).last_error
    assert error["code"] == "requested_url_unanswered"


def test_a_read_url_comparison_with_a_not_mentioned_clause_still_succeeds() -> None:
    """A grounded comparison may say "the material does not mention X"."""
    provider = FakeAnswerProvider(
        f"该协议已读取。对照公司材料，部分条款未提及，但材料与协议比对结论明确。",
        citations=[{"evidence_id": "ev-flow", "quote": "正文"}],
    )
    container, store, _research = build(
        web_intent="web.research",
        answer_provider=provider,
    )
    task = create_task(container, text=f"{URL} 这个网址在讲什么？")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

def test_an_uncited_web_answer_with_no_evidence_is_not_marked_success() -> None:
    provider = FakeAnswerProvider("这是整理后的回答", citations=[])
    container, store, _research = build(
        web_intent="web.research",
        research=EmptyResearch(),
        answer_provider=provider,
    )
    task = create_task(container, text="搜索一下公开资料并总结")

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert (
        store.get_task(task.task_id).last_error["code"]
        == "uncited_web_answer"
    )


def test_a_no_evidence_web_search_completes_as_not_found() -> None:
    provider = FakeAnswerProvider(
        "未找到关于该主题的可靠公开来源。",
        citations=[],
    )
    container, store, _research = build(
        web_intent="web.research",
        research=EmptyResearch(),
        answer_provider=provider,
    )
    task = create_task(container, text="搜索一下北京市网络协议是什么")

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    result = store.get_task(task.task_id).result
    assert "未找到" in result["answer"]
    assert result["citations"] == []


def test_a_to_do_that_mentions_a_link_does_not_run_research() -> None:
    container, store, research = build(
        web_intent="todo.create",
        planner_understanding=understanding("todo.create"),
        llm=ScriptedWebPlanner(compose=True),
        allow_side_effects=True,
    )
    task = create_task(container, text=f"明天看看 {URL} 的排期")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    assert research.requests == []
    assert [item.capability for item in store.list_observations(task.task_id)] == [
        "todo.create"
    ]


def test_the_router_hands_validators_to_the_planner_that_needs_them() -> None:
    """Container wiring reaches through the router into the LLM planner."""

    llm = ScriptedWebPlanner(compose=True)
    container, _store, _research = build(web_intent="web.research", llm=llm)

    assert llm.validators is not None
    assert "web.research" in llm.validators
    assert set(container.registry._capabilities) == {  # noqa: SLF001 - registry surface
        "todo.create",
        "web.research",
        "answer.compose",
    }
