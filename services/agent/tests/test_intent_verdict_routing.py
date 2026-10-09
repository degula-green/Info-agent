"""The classifier's verdict decides the route when the extractors miss.

person.query and report.weekly were regex-only. With them in the intent
contract, a verdict naming either one must produce the corresponding plan even
when the wording is not one the extractors recognise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.capabilities.answer import CAPABILITY_NAME as ANSWER_COMPOSE_NAME
from app.capabilities.person import PERSON_QUERY_NAME
from app.capabilities.report import CAPABILITY_NAME as REPORT_WEEKLY_NAME
from app.kernel.models import (
    CapabilityDescriptor,
    Plan,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
    TaskUnderstanding,
    UnderstandingIntent,
)
from app.planning.knowledge import KnowledgeRoutingPlanner


class StubPlanner:
    """Records whether the LLM planner was reached and what it planned."""

    name = "stub"

    def __init__(self, capability: str, arguments: dict | None = None) -> None:
        self.capability = capability
        self.arguments = dict(arguments or {})
        self.plans = 0
        self.last_call_count = 0

    def create_plan(
        self,
        task,
        capabilities,
        observations,
        constraints=None,
        understanding=None,
        *,
        conversation_context=None,
    ) -> Plan:
        self.plans += 1
        return Plan(
            plan_id="p1",
            task_id=task.task_id,
            objective="stub",
            steps=[
                PlanStep(
                    step_id="s1",
                    plan_id="p1",
                    order=1,
                    capability=self.capability,
                    arguments=dict(self.arguments),
                )
            ],
        )


def _descriptor(name: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        description=name,
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=60,
    )


def _task(text: str, **input_extra) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="t1",
        source_type="chat",
        owner_user_id="u1",
        input={"text": text, **input_extra},
        created_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
    )


def _verdict(name: str, confidence: float = 0.95) -> TaskUnderstanding:
    return TaskUnderstanding(
        is_task=True,
        goal="goal",
        task_kind="answer",
        intent_candidates=[
            UnderstandingIntent(name=name, confidence=confidence)
        ],
        confidence=confidence,
    )


def test_weekly_report_verdict_routes_without_the_regex() -> None:
    # "帮我弄份上周的周报" is not one of the phrasings classify_weekly_report reads.
    stub = StubPlanner(REPORT_WEEKLY_NAME, {"person": "张三", "instruction": "帮我弄份上周的周报"})
    planner = KnowledgeRoutingPlanner(stub, clock=None)

    plan = planner.create_plan(
        _task("帮我弄份上周的周报", attachment_ids=["up-1"]),
        [_descriptor(REPORT_WEEKLY_NAME)],
        [],
        PlanningConstraints(),
        _verdict(REPORT_WEEKLY_NAME),
    )

    assert stub.plans == 1
    assert [step.capability for step in plan.steps] == [REPORT_WEEKLY_NAME]
    # The model never sees the upload handle, so the turn's own template is
    # copied in afterwards.
    assert plan.steps[0].arguments["attachment_ids"] == ["up-1"]


def test_weekly_report_verdict_is_ignored_without_the_capability() -> None:
    stub = StubPlanner(ANSWER_COMPOSE_NAME, {"question": "帮我弄份上周的周报"})
    planner = KnowledgeRoutingPlanner(stub, clock=None)

    plan = planner.create_plan(
        _task("帮我弄份上周的周报", attachment_ids=["up-1"]),
        [_descriptor(ANSWER_COMPOSE_NAME)],
        [],
        PlanningConstraints(),
        _verdict(REPORT_WEEKLY_NAME),
    )

    # No report capability is registered, so nothing may be injected into a
    # step that only looks like a report.
    assert "attachment_ids" not in (plan.steps[0].arguments or {})


def test_person_verdict_routes_without_the_regex() -> None:
    # "张三现在忙不忙" carries no marker the person extractor reads.
    stub = StubPlanner(
        PERSON_QUERY_NAME,
        {"name": "张三", "question": "张三现在忙不忙"},
    )
    planner = KnowledgeRoutingPlanner(stub, clock=None)

    plan = planner.create_plan(
        _task("张三现在忙不忙"),
        [_descriptor(PERSON_QUERY_NAME)],
        [],
        PlanningConstraints(),
        _verdict(PERSON_QUERY_NAME),
    )

    assert stub.plans == 1
    assert [step.capability for step in plan.steps] == [PERSON_QUERY_NAME]


def test_a_weak_verdict_does_not_decide_the_route() -> None:
    stub = StubPlanner(ANSWER_COMPOSE_NAME, {"question": "张三现在忙不忙"})
    planner = KnowledgeRoutingPlanner(stub, clock=None)

    plan = planner.create_plan(
        _task("张三现在忙不忙"),
        [_descriptor(PERSON_QUERY_NAME), _descriptor(ANSWER_COMPOSE_NAME)],
        [],
        PlanningConstraints(),
        _verdict(PERSON_QUERY_NAME, 0.3),
    )

    assert [step.capability for step in plan.steps] == [ANSWER_COMPOSE_NAME]
