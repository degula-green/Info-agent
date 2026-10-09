"""Routing for "写周报": the report must be recognised before the action planner."""

from __future__ import annotations

from datetime import datetime, timezone

from app.capabilities.report import CAPABILITY_NAME
from app.kernel.models import (
    CapabilityDescriptor,
    PlanningConstraints,
    TaskEnvelope,
)
from app.planning.knowledge import (
    KnowledgeRoutingPlanner,
    classify_weekly_report,
)


class NeverPlanner:
    name = "never"

    def create_plan(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the weekly report must not reach the model planner")


def _descriptor(name: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        description=name,
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=90,
    )


def _task(text: str, **input_extra) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-1",
        source_type="chat",
        owner_user_id="user-1",
        input={"text": text, **input_extra},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_classifier_reads_the_person_and_keeps_the_instruction():
    assert classify_weekly_report("给张三写一份周报") == ("张三", "给张三写一份周报")
    assert classify_weekly_report("写张三的周报") == ("张三", "写张三的周报")
    assert classify_weekly_report("按我上传的模板给张三写周报") == (
        "张三",
        "按我上传的模板给张三写周报",
    )


def test_classifier_treats_a_nameless_request_as_the_user_own_report():
    assert classify_weekly_report("给我写周报") == ("我", "给我写周报")
    assert classify_weekly_report("写周报") == ("我", "写周报")


def test_classifier_ignores_sentences_that_only_mention_reports():
    assert classify_weekly_report("统计一下本周周报的数量") is None
    assert classify_weekly_report("张三最近在忙什么") is None


def test_classifier_keeps_names_that_look_like_predicates():
    # "先躺会再说" contains 说; trimming at that character would ask for a
    # different person entirely.
    assert classify_weekly_report("给先躺会再说写一份周报") == (
        "先躺会再说",
        "给先躺会再说写一份周报",
    )


def test_a_measure_word_is_not_read_as_a_person() -> None:
    # "写一份上周的周报" once asked for a person named 一份上周.
    assert classify_weekly_report("写一份上周的周报") == (
        "我",
        "写一份上周的周报",
    )
    assert classify_weekly_report("帮我写一份本周周报") == (
        "我",
        "帮我写一份本周周报",
    )


def test_the_week_wording_is_not_read_as_a_person() -> None:
    # "生成我这一周的周报" once asked for a person named 我这一周.
    assert classify_weekly_report("生成我这一周的周报") == (
        "我",
        "生成我这一周的周报",
    )
    assert classify_weekly_report("帮我生成这周的周报") == (
        "我",
        "帮我生成这周的周报",
    )


def test_the_week_wording_still_survives_for_a_named_person() -> None:
    assert classify_weekly_report("给张三写一份本周周报") == (
        "张三",
        "给张三写一份本周周报",
    )
    assert classify_weekly_report("生成本周周报") == ("我", "生成本周周报")


def test_planner_builds_a_single_report_step_with_the_uploaded_attachment():
    capabilities = [_descriptor(CAPABILITY_NAME)]
    planner = KnowledgeRoutingPlanner(NeverPlanner(), clock=None)
    task = _task(
        "按我上传的模板给张三写周报",
        attachment_ids=["up-1"],
        # A parsed attachment is what the real turn carries; without this the
        # attachment branch is skipped and the test would not pin the order.
        _attachment_excerpt="【周报模板.docx】\n## 周报模板（通用版）",
        _attachment_referenced=True,
    )

    plan = planner.create_plan(
        task, capabilities, [], PlanningConstraints(), None
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [CAPABILITY_NAME]
    assert plan.steps[0].arguments["person"] == "张三"
    assert plan.steps[0].arguments["attachment_ids"] == ["up-1"]
    # Nothing was asked of the model planner.
    assert planner.last_call_count == 0


def test_planner_falls_through_when_the_capability_is_not_registered():
    class RecordingPlanner:
        name = "recording"

        def __init__(self):
            self.plans = 0

        def create_plan(self, task, capabilities, observations, constraints=None, understanding=None):
            from app.kernel.models import Plan

            self.plans += 1
            return Plan(plan_id="p1", task_id=task.task_id, objective="fallback")

    base = RecordingPlanner()
    planner = KnowledgeRoutingPlanner(base, clock=None)
    plan = planner.create_plan(
        _task("给张三写一份周报"),
        [_descriptor("answer.compose")],
        [],
        PlanningConstraints(),
        None,
    )

    # With no report capability registered the turn keeps its normal route.
    assert base.plans == 1
    assert plan.objective == "fallback"


def test_planner_passes_the_turns_week_phrase_to_the_report():
    capabilities = [_descriptor(CAPABILITY_NAME)]
    planner = KnowledgeRoutingPlanner(NeverPlanner(), clock=None)

    plan = planner.create_plan(
        _task("生成我的上上一周的周报"),
        capabilities,
        [],
        PlanningConstraints(),
        None,
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [CAPABILITY_NAME]
    assert plan.steps[0].arguments["time_range"] == "上上一周"
