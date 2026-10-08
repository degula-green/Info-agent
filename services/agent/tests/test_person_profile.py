from __future__ import annotations

from app.capabilities.person_profile import (
    facts_by_person_section,
    merge_facts,
    missing_fact_values,
    question_mode,
    render_person_sections,
    snapshot_fingerprint,
)
from app.providers.person_facts import PersonFact


def _fact(
    fact_type: str,
    value: str,
    *,
    quote: str = "",
    sent_at: str = "2026-10-08T00:00:00+00:00",
    speaker_role: str = "subject",
) -> PersonFact:
    return PersonFact(
        fact_type=fact_type,
        label=fact_type,
        value=value,
        quote=quote or value,
        speaker_role=speaker_role,
        resource_id="resource-1",
        sent_at=sent_at,
    )


def test_question_mode_separates_overview_and_facet() -> None:
    assert question_mode("我想要知道小超的情况").mode == "overview"
    assert question_mode("我想要知道小超的项目情况").facet == "project"
    assert question_mode("杨思琪的联系方式").facet == "contact"
    assert question_mode("小超最近忙什么").facet == "recent_activity"


def test_merge_facts_canonicalizes_and_marks_conflicts() -> None:
    facts = [
        _fact("identity", "李帅", quote="我叫李帅"),
        _fact(
            "identity",
            "李帅",
            quote="我的名字叫李帅",
            sent_at="2026-10-09T00:00:00+00:00",
        ),
        _fact(
            "phone",
            "15325865236",
            sent_at="2026-10-08T00:00:00+00:00",
        ),
        _fact(
            "phone",
            "15325875359",
            sent_at="2026-10-09T00:00:00+00:00",
        ),
    ]
    merged = merge_facts(facts)
    names = [item for item in merged if item["canonical_key"] == "identity.identity"]
    phones = [item for item in merged if item["canonical_key"] == "contact.phone"]
    assert len(names) == 1
    assert len(phones) == 2
    assert [item["status"] for item in phones] == ["current", "historical"]


def test_missing_fact_values_checks_semantic_values() -> None:
    facts = merge_facts(
        [
            _fact("identity", "李帅", quote="我叫李帅"),
            _fact("phone", "15325865236"),
        ]
    )
    assert missing_fact_values("我叫李帅，手机号是15325865236。", facts) == []
    missing = missing_fact_values("只提到李帅。", facts)
    assert [item["value"] for item in missing] == ["15325865236"]


def test_snapshot_fingerprint_is_order_independent() -> None:
    first = snapshot_fingerprint(
        [
            {"chunk_id": "b", "text": "第二条"},
            {"chunk_id": "a", "text": "第一条"},
        ]
    )
    second = snapshot_fingerprint(
        [
            {"chunk_id": "a", "text": "第一条"},
            {"chunk_id": "b", "text": "第二条"},
        ]
    )
    assert first == second


def test_person_sections_omit_empty_and_merge_work_project() -> None:
    facts = merge_facts(
        [
            _fact("phone", "15325865236", sent_at="2026-10-08T00:00:00+00:00"),
            _fact("work", "负责测试", sent_at="2026-10-07T00:00:00+00:00"),
            _fact("project", "支付 demo", sent_at="2026-10-09T00:00:00+00:00"),
        ]
    )
    sections = facts_by_person_section(facts)
    answer = render_person_sections(sections, timezone_name="Asia/Shanghai")

    assert "## 身份" not in answer
    assert "## 联系方式" in answer
    assert "## 工作/项目" in answer
    assert "## 近期活动" not in answer
    assert answer.index("## 联系方式") < answer.index("## 工作/项目")
    assert "负责测试" in answer
    assert "支付 demo" in answer


def test_person_sections_show_historical_then_current() -> None:
    facts = merge_facts(
        [
            _fact("phone", "15325865236", sent_at="2026-10-08T00:00:00+00:00"),
            _fact("phone", "15325875359", sent_at="2026-10-05T00:00:00+00:00"),
        ]
    )
    answer = render_person_sections(
        facts_by_person_section(facts),
        timezone_name="Asia/Shanghai",
    )

    assert answer.index("2026-10-05") < answer.index("2026-10-08")
    assert answer.index("历史") < answer.index("当前")
    assert "15325875359" in answer
    assert "15325865236" in answer


def test_fact_section_uses_neutral_wording_without_internal_attribution() -> None:
    facts = merge_facts(
        [
            _fact(
                "recent_activity",
                "在准备PPT",
                sent_at="2026-10-08T00:00:00+00:00",
                speaker_role="other",
            )
        ]
    )
    answer = render_person_sections(
        facts_by_person_section(facts),
        timezone_name="Asia/Shanghai",
    )

    assert "对话中提到：在准备PPT" in answer
    assert "other_said" not in answer
