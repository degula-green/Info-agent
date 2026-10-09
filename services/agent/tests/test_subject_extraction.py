"""Model-first subject extraction for form/report/person intents."""

from __future__ import annotations

from datetime import datetime, timezone

from app.capabilities.form import extract_subject, resolve_scopes
from app.capabilities.report import CAPABILITY_NAME as REPORT_NAME
from app.kernel.models import (
    CapabilityDescriptor,
    PlanningConstraints,
    TaskEnvelope,
)
from app.planning.knowledge import (
    KnowledgeRoutingPlanner,
    classify_person_question,
    classify_weekly_report,
)
from app.providers.subject import LlmSubjectExtractor
from app.understanding.subject import SubjectMention


class _FakeLLM:
    model = "fake-subject"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.last_call_count = 0
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.last_call_count = 1
        return self.responses.pop(0)

    def complete_structured(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict,
        name: str = "structured_output",
    ) -> str:
        return self.complete(messages)


class _NeverPlanner:
    name = "never"

    def create_plan(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the report plan must not fall through")


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


def _task(text: str) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="subject-test",
        source_type="chat",
        owner_user_id="u1",
        input={"text": text},
        created_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
    )


def test_llm_subject_extractor_returns_a_structured_mention() -> None:
    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"person","mention":"张三","confidence":0.95,"reason":"named"}'
        )
    )

    mention = extractor.extract("给张三写上周周报", intent="report.weekly")

    assert mention == SubjectMention(
        kind="person",
        mention="张三",
        confidence=0.95,
        reason="named",
        source="model",
    )
    assert extractor.last_call_count == 1


def test_llm_subject_extractor_repairs_invalid_json() -> None:
    extractor = LlmSubjectExtractor(
        _FakeLLM(
            "not json",
            '{"kind":"self","mention":"我","confidence":0.9,"reason":"self"}',
        )
    )

    mention = extractor.extract("帮我写一份周报", intent="report.weekly")

    assert mention is not None
    assert mention.kind == "self"
    assert extractor.last_call_count == 2


def test_model_subject_is_used_by_all_three_entry_points() -> None:
    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"person","mention":"某人","confidence":0.95,"reason":"named"}'
        )
    )
    assert (
        extract_subject(
            "帮我把某人的信息填进表格",
            subject_extractor=extractor,
        )
        == "某人"
    )

    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"person","mention":"某人","confidence":0.95,"reason":"named"}'
        )
    )
    assert classify_weekly_report(
        "给某人写上周周报",
        subject_extractor=extractor,
    ) == ("某人", "给某人写上周周报")

    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"person","mention":"某人","confidence":0.95,"reason":"named"}'
        )
    )
    assert classify_person_question(
        "问一下某人最近怎么样",
        subject_extractor=extractor,
    ) == ("某人", "问一下某人最近怎么样")


def test_model_unspecified_is_respected_instead_of_guessed() -> None:
    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"unspecified","mention":"","confidence":0.9,"reason":"no owner"}'
        )
    )

    assert (
        extract_subject(
            "帮我把某人的信息填进表格",
            subject_extractor=extractor,
        )
        == ""
    )
    assert (
        classify_person_question(
            "上上周的情况",
            subject_extractor=extractor,
        )
        is None
    )


def test_invalid_model_mention_falls_back_to_deterministic_reader() -> None:
    # "上上周" is a time phrase, so the model answer must be rejected and the
    # narrow fallback has to choose the user's own report instead.
    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"person","mention":"上上周","confidence":0.99,"reason":"bad"}'
        )
    )

    assert classify_weekly_report(
        "帮我写一份上上周的周报",
        subject_extractor=extractor,
    ) == ("我", "帮我写一份上上周的周报")


def test_time_phrases_never_become_subjects_in_the_fallbacks() -> None:
    assert classify_weekly_report("帮我填写上上一周的周报") == (
        "我",
        "帮我填写上上一周的周报",
    )
    assert classify_weekly_report("帮我填写我的上上一周的周报") == (
        "我",
        "帮我填写我的上上一周的周报",
    )
    assert extract_subject("帮我把我的上上一周的信息填进表格") == "我"
    assert extract_subject("帮我把上上一周的信息填进表格") == ""
    assert extract_subject("帮我把张三上上周的信息填进表格") == "张三"
    assert classify_person_question("上上一周的情况") is None
    assert classify_person_question("问一下上上一周张三的情况") == (
        "张三",
        "问一下上上一周张三的情况",
    )
    assert classify_person_question("帮我查一下上周的情况") is None


def test_form_scope_inference_still_owns_the_no_subject_case() -> None:
    assert resolve_scopes("帮我把上上一周的信息填进表格", ["姓名"]) == ["我"]
    assert resolve_scopes("帮我把信息填写进表格", ["姓名"]) == ["我"]


def test_planner_charges_the_subject_model_call() -> None:
    extractor = LlmSubjectExtractor(
        _FakeLLM(
            '{"kind":"person","mention":"张三","confidence":0.95,"reason":"named"}'
        )
    )
    planner = KnowledgeRoutingPlanner(
        _NeverPlanner(),
        subject_extractor=extractor,
    )

    plan = planner.create_plan(
        _task("给张三写本周周报"),
        [_descriptor(REPORT_NAME)],
        [],
        PlanningConstraints(),
        None,
    )

    assert plan.steps[0].arguments["person"] == "张三"
    assert planner.last_call_count == 1
