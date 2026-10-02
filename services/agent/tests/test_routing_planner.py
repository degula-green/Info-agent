"""Routing: only chat turns that need composition reach the model.

The information-collection entry is a fixed pipeline, and a to-do must not
become a model decision just because the sentence mentioned a website. These
tests pin which planner each shape of Task gets.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.kernel.models import (
    Plan,
    PlannerDecision,
    PlanningConstraints,
    TaskEnvelope,
    TaskUnderstanding,
    UnderstandingIntent,
)
from app.planning.routing import RoutingPlanner


class RecordingPlanner:
    """Counts which planner the router picked; it plans nothing real."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.plans = 0
        self.decisions = 0
        self.validators: dict | None = None

    def set_validators(self, validators: dict) -> None:
        self.validators = dict(validators)

    def create_plan(
        self, task, capabilities, observations, constraints=None, understanding=None
    ) -> Plan:
        self.plans += 1
        return Plan(
            plan_id=f"{self.label}-plan",
            task_id=task.task_id,
            objective=self.label,
        )

    def decide_after_observation(
        self, task, current_plan, observations, constraints, understanding=None
    ) -> PlannerDecision:
        self.decisions += 1
        return PlannerDecision(action="complete")


def envelope(*, text: str = "随便一句", source_type: str = "chat") -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-1",
        source_type=source_type,
        owner_user_id="user-1",
        input={"text": text},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def understanding(*names: str, is_task: bool = True) -> TaskUnderstanding:
    return TaskUnderstanding(
        is_task=is_task,
        goal="测试",
        intent_candidates=[
            UnderstandingIntent(name=name, confidence=0.9) for name in names
        ],
    )


def router() -> tuple[RoutingPlanner, RecordingPlanner, RecordingPlanner]:
    deterministic = RecordingPlanner("deterministic")
    llm = RecordingPlanner("llm")
    return (
        RoutingPlanner(deterministic=deterministic, llm=llm),
        deterministic,
        llm,
    )


def test_a_collected_message_never_reaches_the_llm_planner() -> None:
    """Even a collected URL stays on the fixed pipeline."""

    planner, deterministic, llm = router()

    plan = planner.create_plan(
        envelope(text="看看 https://example.com/standup", source_type="knowledge_event"),
        [],
        [],
        PlanningConstraints(),
        understanding("web.research"),
    )

    assert plan.objective == "deterministic"
    assert (deterministic.plans, llm.plans) == (1, 0)


def test_a_chat_web_request_reaches_the_llm_planner() -> None:
    planner, deterministic, llm = router()

    plan = planner.create_plan(
        envelope(text="搜索飞书开放平台的消息卡片回调"),
        [],
        [],
        PlanningConstraints(),
        understanding("web.research"),
    )

    assert plan.objective == "llm"
    assert (deterministic.plans, llm.plans) == (0, 1)


def test_a_chat_to_do_stays_on_the_deterministic_planner() -> None:
    planner, deterministic, llm = router()

    planner.create_plan(
        envelope(text="明天下午三点跟张三开评审会"),
        [],
        [],
        PlanningConstraints(),
        understanding("todo.create"),
    )

    assert (deterministic.plans, llm.plans) == (1, 0)


def test_an_intent_this_document_does_not_own_stays_deterministic() -> None:
    """With no retrieval tools registered, knowledge.answer has no LLM plan."""

    planner, deterministic, llm = router()

    planner.create_plan(
        envelope(text="公司的办公地址是什么"),
        [],
        [],
        PlanningConstraints(),
        understanding("knowledge.answer"),
    )

    assert (deterministic.plans, llm.plans) == (1, 0)


def test_a_knowledge_question_reaches_the_llm_planner_when_configured() -> None:
    """With retrieval tools registered, knowledge.answer needs a composed plan.

    The deterministic planner can only answer a document question from an
    attachment; a question about collected company data has to be composed from
    search_sources -> search_content -> knowledge.answer.
    """

    deterministic = RecordingPlanner("deterministic")
    llm = RecordingPlanner("llm")
    planner = RoutingPlanner(
        deterministic=deterministic,
        llm=llm,
        llm_intents=frozenset({"web.research", "knowledge.answer"}),
    )

    plan = planner.create_plan(
        envelope(text="昨天晚上10点aims群里在聊什么"),
        [],
        [],
        PlanningConstraints(),
        understanding("knowledge.answer"),
    )

    assert plan.objective == "llm"
    assert (deterministic.plans, llm.plans) == (0, 1)


def test_no_understanding_keeps_the_previous_behaviour() -> None:
    planner, deterministic, llm = router()

    planner.create_plan(envelope(), [], [], PlanningConstraints(), None)

    assert (deterministic.plans, llm.plans) == (1, 0)


def test_follow_up_decisions_use_the_same_planner_as_the_plan() -> None:
    planner, deterministic, llm = router()
    task = envelope(text="搜索公开资料")
    plan = planner.create_plan(
        task, [], [], PlanningConstraints(), understanding("web.research")
    )

    planner.decide_after_observation(
        task, plan, [], PlanningConstraints(), understanding("web.research")
    )

    assert (deterministic.decisions, llm.decisions) == (0, 1)


def test_validators_are_handed_to_the_planner_that_needs_them() -> None:
    """Only the LLM planner re-asks on schema misses, so only it needs them."""

    planner, deterministic, llm = router()
    sentinel = {"web.research": object()}

    planner.set_validators(sentinel)

    assert llm.validators == sentinel
    assert deterministic.validators is None


def test_a_planner_without_set_validators_is_tolerated() -> None:
    """A stub planner that never re-asks must not break container wiring."""

    class Plain:
        def create_plan(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError

        def decide_after_observation(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError

    planner = RoutingPlanner(deterministic=Plain(), llm=Plain())

    planner.set_validators({"x": object()})  # must not raise
