"""Step-4 execution: reference chain, evidence, budget and timeout."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.capabilities.answer import AnswerComposeCapability
from app.capabilities.web import WebExtractCapability, WebFetchCapability
from app.capabilities.web_research import WebResearchCapability
from app.container import build_container
from app.infrastructure.web.content_reader import ContentReader
from app.kernel.errors import PermanentCapabilityError
from app.kernel.models import (
    CapabilityDescriptor,
    Plan,
    PlannerDecision,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
)
from app.kernel.registry import CapabilityRegistry
from app.planning.llm import (
    OpenAICompatiblePlanner,
    PlannerValidationError,
    unresolved_references,
)
from app.policy.descriptor import DescriptorPolicy
from app.testing.fake_providers import FakeAnswerProvider, FakePageFetcher
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from tests.support import create_task, make_settings

URL = "https://93.184.216.34/page"
HTML = (
    "<html><head><title>示例页面</title></head>"
    "<body><p>这是正文第一段。</p><a href='/next'>下一页</a></body></html>"
)


def envelope(text: str = "读一下这个网页") -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-step4",
        source_type="chat",
        owner_user_id="user-1",
        input={"text": text},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class ReferenceChainPlanner:
    """Plans fetch -> extract -> compose with real cross-step references."""

    name = "scripted"
    plan_id = "plan-chain"

    def __init__(self, url: str = URL, question: str = "这个页面讲了什么") -> None:
        self.url = url
        self.question = question

    def create_plan(
        self, task, capabilities, observations, constraints=None, understanding=None
    ) -> Plan:
        plan_id = self.plan_id
        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective="读网页并回答",
            steps=[
                PlanStep(
                    step_id=f"{plan_id}-step-1",
                    plan_id=plan_id,
                    order=1,
                    capability="web.fetch",
                    arguments={"url": self.url},
                ),
                PlanStep(
                    step_id=f"{plan_id}-step-2",
                    plan_id=plan_id,
                    order=2,
                    capability="web.extract",
                    arguments={
                        "document": f"$steps.{plan_id}-step-1.output.content",
                        "url": self.url,
                    },
                ),
                PlanStep(
                    step_id=f"{plan_id}-step-3",
                    plan_id=plan_id,
                    order=3,
                    capability="answer.compose",
                    arguments={
                        "question": self.question,
                        "evidence": f"$steps.{plan_id}-step-2.output.evidence",
                    },
                ),
            ],
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        if any(
            step.status in {"pending", "ready", "running"}
            for step in current_plan.steps
        ):
            return PlannerDecision(action="continue")
        return PlannerDecision(action="complete")


class FailPlanner(ReferenceChainPlanner):
    """Runs one scripted step, then gives up."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__()
        self.steps = steps

    def create_plan(
        self, task, capabilities, observations, constraints=None, understanding=None
    ) -> Plan:
        plan_id = "plan-slow"
        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective="慢调用",
            steps=[
                PlanStep(
                    step_id=f"{plan_id}-step-{index}",
                    plan_id=plan_id,
                    order=index,
                    capability=item["capability"],
                    arguments=dict(item.get("arguments") or {}),
                )
                for index, item in enumerate(self.steps, start=1)
            ],
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        return PlannerDecision(action="fail", reason="没有可走的路")


class SlowCapability:
    """Always finishes, never inside its own descriptor budget."""

    descriptor = CapabilityDescriptor(
        name="test.slow",
        description="over-budget on purpose",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=1,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True}


def build_step4_container(
    *,
    fetcher=None,
    answer_provider=None,
    planner=None,
    extra_capabilities=(),
    research=False,
    search_provider=None,
    **overrides,
):
    settings = make_settings(**overrides)
    page_fetcher = fetcher or FakePageFetcher()
    capabilities = [
        WebFetchCapability(page_fetcher),
        WebExtractCapability(),
        AnswerComposeCapability(answer_provider or FakeAnswerProvider()),
        *extra_capabilities,
    ]
    if research:
        # The kernel tests keep the fetch/extract pair so they can drive the
        # Runtime step by step; the research capability is added where a test
        # exercises the Planner-visible surface.
        capabilities.append(
            WebResearchCapability(
                ContentReader(page_fetcher, renderer=None, min_text_chars=1),
                search_provider=search_provider,
                aliases={},
            )
        )
    registry = CapabilityRegistry(capabilities)
    store = InMemoryAgentStore()
    container = build_container(
        settings=settings,
        store=store,
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=planner or ReferenceChainPlanner(),
        policy=DescriptorPolicy(registry),
    )
    return container, store


def test_fetch_extract_compose_runs_end_to_end() -> None:
    fetcher = FakePageFetcher({URL: {"content": HTML}})
    provider = FakeAnswerProvider("页面介绍的是示例内容", model_calls=2)
    container, store = build_step4_container(
        fetcher=fetcher, answer_provider=provider, task_max_model_calls=24
    )
    task = create_task(container, text="读一下这个网页并总结")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert stored.result["answer"] == "页面介绍的是示例内容"
    citation = stored.result["citations"][0]
    assert citation["evidence_id"].startswith("ev-")

    completed = [
        event
        for event in store.list_events(task.task_id)
        if event.event_type == "task.completed"
    ][-1]
    assert completed.payload["answer"] == "页面介绍的是示例内容"
    assert completed.payload["citations"][0]["evidence_id"] == citation["evidence_id"]

    extract = [
        item
        for item in store.list_observations(task.task_id)
        if item.capability == "web.extract"
    ][0]
    assert extract.evidence[0]["url"] == URL
    assert extract.evidence[0]["evidence_id"] == citation["evidence_id"]
    assert [item.payload["evidence_id"] for item in store.evidence] == [
        citation["evidence_id"]
    ]

    # The answer step pays for its own model calls out of the Task budget.
    assert stored.model_call_count == 2


def test_over_budget_calls_are_retried_in_place_then_handed_to_the_planner(
    monkeypatch,
) -> None:
    """A capability that ignores its own timeout cannot silently succeed."""

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = itertools.count()
    monkeypatch.setattr(
        "app.kernel.executor._now",
        lambda: base + timedelta(seconds=5 * next(ticks)),
    )
    slow = SlowCapability()
    container, store = build_step4_container(
        extra_capabilities=[slow],
        planner=FailPlanner([{"capability": "test.slow", "arguments": {}}]),
    )
    task = create_task(container, text="跑一个超时的能力")

    assert container.execution_service.run_task(task.task_id) == "failed"

    assert slow.calls == 3  # max_step_attempts, all in place
    failed = [
        item
        for item in store.list_observations(task.task_id)
        if item.capability == "test.slow"
    ]
    assert [item.status for item in failed] == ["failed", "failed", "failed"]
    assert failed[0].error["classification"] == "retryable_error"
    assert failed[0].error["message"] == "capability exceeded its timeout budget"
    decisions = [
        event
        for event in store.list_events(task.task_id)
        if event.event_type == "planner.decision"
    ]
    assert len(decisions) == 1  # only after the retry budget ran out
    assert store.get_task(task.task_id).model_call_count == 0

    # One call row, therefore one request_id, reused by every in-place retry.
    rows = list(store.calls.values())
    assert len(rows) == 1
    assert rows[0].attempt == 3


class StubPlannerClient:
    model = "stub-planner"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []
        self.last_call_count = 1

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.outputs.pop(0)


def reference_descriptors() -> list[CapabilityDescriptor]:
    return [
        WebFetchCapability(FakePageFetcher()).descriptor,
        WebExtractCapability().descriptor,
    ]


BAD_REFERENCE_PLAN = (
    '{"objective": "抓取并阅读", "steps": ['
    '{"capability": "web.fetch", "arguments": {"url": "https://93.184.216.34/page"}}, '
    '{"capability": "web.extract", '
    '"arguments": {"document": "$steps.1.output.content"}}]}'
)

GOOD_REFERENCE_PLAN = (
    '{"objective": "抓取并阅读", "steps": ['
    '{"capability": "web.fetch", "arguments": {"url": "https://93.184.216.34/page"}}, '
    '{"capability": "web.extract", '
    '"arguments": {"document": "$steps.plan-fixed-step-1.output.content"}}]}'
)


def test_a_bad_reference_is_repaired_once(monkeypatch) -> None:
    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    client = StubPlannerClient([BAD_REFERENCE_PLAN, GOOD_REFERENCE_PLAN])
    planner = OpenAICompatiblePlanner(client)

    plan = planner.create_plan(
        envelope(), reference_descriptors(), [], PlanningConstraints()
    )

    assert len(client.calls) == 2
    assert (
        plan.steps[1].arguments["document"]
        == "$steps.plan-fixed-step-1.output.content"
    )


def test_a_plan_that_keeps_a_bad_reference_fails(monkeypatch) -> None:
    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    client = StubPlannerClient([BAD_REFERENCE_PLAN, BAD_REFERENCE_PLAN])
    planner = OpenAICompatiblePlanner(client)

    with pytest.raises(PlannerValidationError) as excinfo:
        planner.create_plan(envelope(), reference_descriptors(), [], PlanningConstraints())

    assert "$steps.1.output.content" in str(excinfo.value)
    assert len(client.calls) == 2


def test_the_prompt_names_the_step_ids_the_model_must_write(monkeypatch) -> None:
    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    client = StubPlannerClient([GOOD_REFERENCE_PLAN])
    planner = OpenAICompatiblePlanner(client)

    planner.create_plan(envelope(), reference_descriptors(), [], PlanningConstraints())

    system = client.calls[0][0]["content"]
    assert "plan-fixed-step-" in system
    assert "evidence_ref" in system
    assert '"step"' in system and '"output"' in system


def test_a_replan_that_echoes_step_ids_is_still_accepted(monkeypatch) -> None:
    """The prompt names the ids, so a model echoes one back inside a step."""

    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    decision_json = (
        '{"action": "replan", "reason": "换一条路", "steps": ['
        '{"step_id": "plan-fixed-step-1", "capability": "web.fetch", '
        '"arguments": {"url": "https://93.184.216.34/page"}}]}'
    )
    client = StubPlannerClient([decision_json])
    planner = OpenAICompatiblePlanner(client)
    current = Plan(plan_id="plan-old", task_id="task-step4", objective="旧的", steps=[])

    decision = planner.decide_after_observation(
        envelope(), current, [], PlanningConstraints()
    )

    assert len(client.calls) == 1  # no repair round needed
    assert decision.action == "replan"
    assert decision.plan is not None
    assert decision.plan.steps[0].step_id == "plan-fixed-step-1"


def test_a_complete_decision_that_restates_the_answer_is_accepted() -> None:
    """The model echoed the finished answer back; the Observation already has it."""

    decision_json = (
        '{"action": "complete", "reason": "已经回答", '
        '"output": {"answer": "页面介绍了 Example Domain。"}}'
    )
    client = StubPlannerClient([decision_json])
    planner = OpenAICompatiblePlanner(client)
    current = Plan(plan_id="plan-old", task_id="task-step4", objective="旧的", steps=[])

    decision = planner.decide_after_observation(
        envelope(), current, [], PlanningConstraints()
    )

    assert len(client.calls) == 1
    assert decision.action == "complete"


def test_a_forward_reference_is_refused() -> None:
    plan = Plan(
        plan_id="p",
        task_id="t",
        objective="o",
        steps=[
            PlanStep(
                step_id="p-step-1",
                plan_id="p",
                order=1,
                capability="web.extract",
                arguments={"document": "$steps.p-step-2.output.text"},
            ),
            PlanStep(
                step_id="p-step-2",
                plan_id="p",
                order=2,
                capability="web.fetch",
                arguments={"url": URL},
            ),
        ],
    )

    problems = unresolved_references(plan, [])

    assert problems and "p-step-2" in problems[0]


def test_a_reference_to_a_missing_observation_is_refused() -> None:
    plan = Plan(
        plan_id="p",
        task_id="t",
        objective="o",
        steps=[
            PlanStep(
                step_id="p-step-1",
                plan_id="p",
                order=1,
                capability="web.extract",
                arguments={"document": "$observations.ghost.output.text"},
            ),
        ],
    )

    problems = unresolved_references(plan, [])

    assert problems and "ghost" in problems[0]


def test_the_task_fails_with_the_offending_reference(monkeypatch) -> None:
    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    client = StubPlannerClient([BAD_REFERENCE_PLAN, BAD_REFERENCE_PLAN])
    container, store = build_step4_container(
        planner=OpenAICompatiblePlanner(client)
    )
    task = create_task(container, text="抓取并阅读")

    assert container.execution_service.run_task(task.task_id) == "failed"

    last_error = store.get_task(task.task_id).last_error
    assert "$steps.1.output.content" in last_error["message"]


class PermanentCapability:
    """Fails on purpose so a re-plan is triggered from a real Observation."""

    descriptor = CapabilityDescriptor(
        name="test.permanent",
        description="always fails",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise PermanentCapabilityError("cannot be done")


class FetchThenReplanPlanner:
    """Fails once, then re-plans into several consecutive fetches."""

    name = "scripted"

    def __init__(self, urls: list[str]) -> None:
        self.urls = list(urls)
        self.replanned = False

    def create_plan(
        self, task, capabilities, observations, constraints=None, understanding=None
    ) -> Plan:
        plan_id = "plan-first"
        return Plan(
            plan_id=plan_id,
            task_id=task.task_id,
            objective="先失败一次",
            steps=[
                PlanStep(
                    step_id=f"{plan_id}-step-1",
                    plan_id=plan_id,
                    order=1,
                    capability="test.permanent",
                    arguments={},
                )
            ],
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        latest = observations[-1] if observations else None
        if latest is not None and latest.status == "failed" and not self.replanned:
            self.replanned = True
            plan_id = "plan-second"
            return PlannerDecision(
                action="replan",
                reason="换一条路",
                plan=Plan(
                    plan_id=plan_id,
                    task_id=task.task_id,
                    parent_plan_id=current_plan.plan_id,
                    triggered_by_observation_id=latest.observation_id,
                    objective="连续抓取",
                    steps=[
                        PlanStep(
                            step_id=f"{plan_id}-step-{index}",
                            plan_id=plan_id,
                            order=index,
                            capability="web.fetch",
                            arguments={"url": url},
                        )
                        for index, url in enumerate(self.urls, start=1)
                    ],
                ),
            )
        if any(
            step.status in {"pending", "ready", "running"}
            for step in current_plan.steps
        ):
            return PlannerDecision(action="continue")
        return PlannerDecision(action="complete")


def test_three_consecutive_fetches_respect_the_configured_limit() -> None:
    """A research plan may fetch several pages; only a loop is stopped."""

    urls = [f"https://93.184.216.34/p{index}" for index in (1, 2, 3)]
    pages = {url: {"content": HTML} for url in urls}

    fetcher = FakePageFetcher(dict(pages))
    container, store = build_step4_container(
        fetcher=fetcher,
        extra_capabilities=[PermanentCapability()],
        planner=FetchThenReplanPlanner(urls),
    )
    task = create_task(container, text="连续抓三个页面")

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert len(fetcher.calls) == 3

    fetcher = FakePageFetcher(dict(pages))
    container, store = build_step4_container(
        fetcher=fetcher,
        extra_capabilities=[PermanentCapability()],
        planner=FetchThenReplanPlanner(urls),
        task_max_same_capability_calls=2,
    )
    task = create_task(container, text="连续抓三个页面")

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert len(fetcher.calls) == 2
    message = store.get_task(task.task_id).last_error["message"]
    assert "连续尝试" in message


def test_the_container_registers_the_planner_visible_capabilities() -> None:
    from app.container import build_registry
    from app.testing.in_memory_todo_store import InMemoryTodoStore

    settings = make_settings(web_timeout_seconds=7, answer_timeout_seconds=42)
    registry = build_registry(settings, InMemoryTodoStore())

    descriptors = registry.list_descriptors()
    assert sorted(item.name for item in descriptors) == [
        "answer.compose",
        "chat.reply",
        "todo.create",
        "web.research",
    ]
    for descriptor in descriptors:
        assert descriptor.input_schema
        assert descriptor.output_schema

    # The Planner sees one public-web capability; the fetcher's own budget is
    # still wired to the same setting, so the descriptor the Runtime checks and
    # the socket the provider opens cannot drift apart.
    research = registry.get("web.research")
    assert research.reader.fetcher.timeout_seconds == 7
    assert registry.get("answer.compose").descriptor.timeout_seconds == 42

    # The reply capability is the only moving part of its own switch: with it
    # off the planner simply has nothing to call, which is the old behaviour.
    without_reply = build_registry(
        make_settings(chat_reply_enabled=False), InMemoryTodoStore()
    )
    assert "chat.reply" not in {item.name for item in without_reply.list_descriptors()}


STRICT_REFERENCE_PLAN = (
    '{"objective": "读网页并回答", "steps": ['
    '{"capability": "web.research", "arguments": '
    '{"request": "Python json documentation", "urls": ["' + URL + '"]}}, '
    '{"capability": "answer.compose", "arguments": '
    '{"question": "这个页面讲了什么", '
    '"evidence_ref": {"step": 1, "output": "evidence"}}}]}'
)


def test_a_plan_written_with_strict_references_runs_end_to_end() -> None:
    """The model names the steps; the binder and Runtime do the rest.

    This is the shape the strict Planner schema asks for: no copied page text
    and no hand-written ``$steps`` path, just the index of the step that has
    the value.
    """

    fetcher = FakePageFetcher({URL: {"content": HTML}})
    provider = FakeAnswerProvider("页面介绍的是示例内容")
    client = StubPlannerClient(
        [
            STRICT_REFERENCE_PLAN,
            '{"action": "continue", "reason": "还有步骤", "steps": []}',
            '{"action": "complete", "reason": "已经回答"}',
        ]
    )
    container, store = build_step4_container(
        fetcher=fetcher,
        answer_provider=provider,
        planner=OpenAICompatiblePlanner(client),
        task_max_model_calls=24,
        research=True,
    )
    task = create_task(container, text=f"读一下 {URL} 并总结")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert stored.result["answer"] == "页面介绍的是示例内容"
    research = [
        item
        for item in store.list_observations(task.task_id)
        if item.capability == "web.research"
    ][0]
    assert research.evidence[0]["url"] == URL
    # The answer step really received the fetched page's evidence.
    assert provider.calls[0][1][0]["evidence_id"] == research.evidence[0]["evidence_id"]


def test_a_strict_reference_plan_is_bound_before_the_runtime_sees_it() -> None:
    """The planner-facing object becomes a $steps path inside the planner."""

    client = StubPlannerClient([STRICT_REFERENCE_PLAN])
    planner = OpenAICompatiblePlanner(client)
    descriptors = [
        WebResearchCapability(
            ContentReader(FakePageFetcher(), renderer=None, min_text_chars=1),
            search_provider=None,
            aliases={},
        ).descriptor,
        AnswerComposeCapability(FakeAnswerProvider()).descriptor,
    ]

    task = envelope()
    plan = planner.create_plan(task, descriptors, [], PlanningConstraints())

    assert len(client.calls) == 1  # binding is not a model round trip
    assert plan.steps[0].arguments == {
        # The model paraphrased the request; the planner replaces it with the
        # user's own words, because web.research trusts URLs found in there.
        "request": "读一下这个网页",
        "urls": [URL],
    }
    assert plan.steps[1].arguments["evidence"] == (
        f"$steps.{plan.plan_id}-step-1.output.evidence"
    )
    assert all(
        "document_ref" not in step.arguments and "evidence_ref" not in step.arguments
        for step in plan.steps
    )


class ContinueWithNothingLeftPlanner:
    """Plans one read, then says "continue" with nothing left to run.

    Observed from qwen-plus: after web.research succeeded it answered
    ``continue`` even though the plan had no further steps. The Runtime used to
    fail the Task there, throwing away work that had already finished.
    """

    name = "continue-nothing"
    plan_id = "plan-continue"

    def create_plan(
        self, task, capabilities, observations, constraints=None, understanding=None
    ) -> Plan:
        return Plan(
            plan_id=self.plan_id,
            task_id=task.task_id,
            objective="读一页",
            steps=[
                PlanStep(
                    step_id=f"{self.plan_id}-step-1",
                    plan_id=self.plan_id,
                    order=1,
                    capability="web.fetch",
                    arguments={"url": URL},
                )
            ],
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        return PlannerDecision(action="continue")


def test_a_continue_with_nothing_left_completes_instead_of_failing() -> None:
    container, store = build_step4_container(
        fetcher=FakePageFetcher({URL: {"content": HTML}}),
        planner=ContinueWithNothingLeftPlanner(),
    )
    task = create_task(container, text="读一页")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert [item.status for item in store.list_observations(task.task_id)] == [
        "succeeded"
    ]
    assert any("no pending step" in item for item in record.result["warnings"])


class RetryablePlannerError(RuntimeError):
    """A transport-level Planner failure; the kernel may retry it."""

    classification = "retryable_error"


class FlakyDecisionPlanner:
    """Plans one read, then fails the closing decision a number of times."""

    name = "flaky-decision"
    plan_id = "plan-flaky"

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.decisions = 0

    def create_plan(
        self, task, capabilities, observations, constraints=None, understanding=None
    ) -> Plan:
        return Plan(
            plan_id=self.plan_id,
            task_id=task.task_id,
            objective="读一页",
            steps=[
                PlanStep(
                    step_id=f"{self.plan_id}-step-1",
                    plan_id=self.plan_id,
                    order=1,
                    capability="web.fetch",
                    arguments={"url": URL},
                )
            ],
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        self.decisions += 1
        if self.failures > 0:
            self.failures -= 1
            raise RetryablePlannerError("remote HTTP request failed")
        return PlannerDecision(action="complete")


def test_a_transient_planner_failure_is_retried() -> None:
    """One dropped connection is not a reason to fail the whole Task."""

    planner = FlakyDecisionPlanner(failures=1)
    container, store = build_step4_container(
        fetcher=FakePageFetcher({URL: {"content": HTML}}), planner=planner
    )
    task = create_task(container, text="读一页")

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert planner.decisions == 2  # the first attempt failed, the retry answered


def test_a_planner_that_stays_down_does_not_discard_finished_work() -> None:
    """Observed in production: both steps succeeded, the closing decision call
    hit "remote HTTP request failed", and the finished Task was reported failed."""

    planner = FlakyDecisionPlanner(failures=99)
    container, store = build_step4_container(
        fetcher=FakePageFetcher({URL: {"content": HTML}}), planner=planner
    )
    task = create_task(container, text="读一页")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert [item.status for item in store.list_observations(task.task_id)] == [
        "succeeded"
    ]
    assert any("planner unavailable" in item for item in record.result["warnings"])


class UnparseablePlannerError(RuntimeError):
    """The model answered something the Planner could not read (truncated JSON)."""

    classification = "permanent_error"


class UnreadableDecisionPlanner(FlakyDecisionPlanner):
    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        self.decisions += 1
        raise UnparseablePlannerError(
            "planner output could not be parsed: invalid JSON: Unterminated string"
        )


def test_an_unreadable_closing_decision_still_delivers_the_work() -> None:
    """Observed: the model truncated its JSON on the closing call, and a finished
    Task was reported failed even though answer.compose had already succeeded."""

    planner = UnreadableDecisionPlanner(failures=0)
    container, store = build_step4_container(
        fetcher=FakePageFetcher({URL: {"content": HTML}}), planner=planner
    )
    task = create_task(container, text="读一页")

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    record = store.get_task(task.task_id)
    assert [item.status for item in store.list_observations(task.task_id)] == [
        "succeeded"
    ]
    assert any("planner unavailable" in item for item in record.result["warnings"])


def test_a_failed_step_is_still_a_failure_when_the_planner_is_down() -> None:
    """The salvage only applies to finished work, never to a broken step."""

    planner = UnreadableDecisionPlanner(failures=0)
    container, store = build_step4_container(
        # No canned page for this URL: the fetch step fails permanently.
        fetcher=FakePageFetcher(),
        planner=planner,
    )
    task = create_task(container, text="读一页")

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert store.get_task(task.task_id).last_error is not None
