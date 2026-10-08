"""Organization subjects: who answers them, and what scope a form reads.

A company is not a person. The route to the knowledge tools is decided from
the name's shape -- never from "ResolvePerson found nothing", because an
unattached person looks exactly the same. Form filling reads a person's three
sources and looks up a company by name, with the full name pinned in the body.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.capabilities.answer import CAPABILITY_NAME as ANSWER_COMPOSE_NAME
from app.capabilities.form import (
    DEFAULT_COMPANY_SCOPE,
    extract_subject,
    infer_scopes,
    resolve_scopes,
)
from app.capabilities.knowledge import (
    KNOWLEDGE_ANSWER_NAME,
    SEARCH_CONTENT_NAME,
    SEARCH_SOURCES_NAME,
)
from app.capabilities.person import PERSON_QUERY_NAME
from app.infrastructure.rag.form_retriever import (
    KnowledgeFormRetriever,
    RetrievedChunk,
    ScopeResolution,
)
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
from app.understanding.subject import (
    organization_subject_in,
    subject_core,
    subject_is_organization,
)

# ---------------------------------------------------------------------------
# Subject shape
# ---------------------------------------------------------------------------


def test_a_trailing_organization_word_is_an_organization() -> None:
    assert subject_is_organization("深空公司")
    assert subject_is_organization("公司")
    assert subject_is_organization("中国银行")
    assert subject_is_organization("我的公司")
    assert subject_is_organization("我们公司")


def test_a_person_is_not_an_organization() -> None:
    assert not subject_is_organization("小李")
    assert not subject_is_organization("张三")
    assert not subject_is_organization("小呆呆仓鼠")
    # A company label in front of a person's name is still a person: this is
    # exactly the row the knowledge service reduces to "张三".
    assert not subject_is_organization("飞鱼公司张三")


def test_a_self_reference_prefix_narrows_to_the_bare_word() -> None:
    assert subject_core("我们的公司") == "公司"
    assert subject_core("我司") == "我司"
    assert subject_core("小李") == "小李"


def test_the_organization_is_read_out_of_a_sentence() -> None:
    assert organization_subject_in("深空公司的情况是什么") == "深空公司"
    assert organization_subject_in("深空公司那边最近怎么样") == "深空公司"
    assert organization_subject_in("帮我把深空公司信息填进表格") == "深空公司"
    assert organization_subject_in("小李的情况是什么") == ""


# ---------------------------------------------------------------------------
# Form subject extraction and defaults
# ---------------------------------------------------------------------------


def test_the_subject_is_read_with_and_without_the_particle() -> None:
    assert extract_subject("帮我把深空公司的信息填进表格") == "深空公司"
    assert extract_subject("帮我把深空公司信息填进表格") == "深空公司"
    assert extract_subject("帮我把公司的信息填进表格") == "公司"
    assert extract_subject("帮我把我的信息填入表单") == "我"


def test_a_sentence_without_a_subject_still_reads_none() -> None:
    # "把信息填写进表格" has no subject to capture; the columns decide instead.
    assert extract_subject("帮我把信息填写进表格") == ""
    assert resolve_scopes("帮我把信息填写进表格", ["姓名"]) == ["我"]


def test_the_inferred_company_scope_is_the_bare_word() -> None:
    assert DEFAULT_COMPANY_SCOPE == "公司"
    assert infer_scopes(["公司名称", "统一社会信用代码"]) == ["公司"]
    assert infer_scopes(["姓名", "公司名称"]) == ["我", "公司"]
    assert infer_scopes(["项目名称", "负责人"]) == ["我"]


# ---------------------------------------------------------------------------
# Form retriever: person groups vs company name search
# ---------------------------------------------------------------------------


class _SourcesCapability:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def validate(self, arguments: dict) -> dict:
        return dict(arguments)

    def execute(self, arguments: dict) -> dict:
        self.calls.append(dict(arguments))
        return {"sources": [], "resource_ids": []}


class _PersonScopeStub:
    def __init__(self, groups=(), subject: str = "") -> None:
        self.groups = tuple(groups)
        self.subject = subject
        self.names: list[str] = []

    def resolve(self, name, **kwargs):
        from app.infrastructure.rag.form_retriever import PersonScopeResult

        self.names.append(name)
        return PersonScopeResult(subject=self.subject or name, groups=self.groups)


def test_the_company_lookup_pins_the_full_name_in_the_body() -> None:
    sources = _SourcesCapability()
    person = _PersonScopeStub()
    retriever = KnowledgeFormRetriever(
        sources, sources, person_scope=person
    )

    retriever.resolve_scope("深空公司", owner_user_id="u1")

    # A company is never resolved as a person...
    assert person.names == []
    # ...and the search demands the whole name, not just the word "公司".
    assert sources.calls[0]["content_contains"] == ["深空公司"]
    assert sources.calls[0]["query"] == "深空公司"


def test_a_person_is_resolved_through_his_three_sources() -> None:
    sources = _SourcesCapability()
    person = _PersonScopeStub(groups=(("sent", ("res-1",)),))
    retriever = KnowledgeFormRetriever(sources, sources, person_scope=person)

    resolution = retriever.resolve_scope("小李", owner_user_id="u1")

    assert person.names == ["小李"]
    assert resolution.resource_ids == ("res-1",)
    assert dict(resolution.groups)["sent"] == ("res-1",)
    # No name search happened: the person path already knows his resources.
    assert sources.calls == []


class _GroupedRetriever:
    """Returns canned text per resource id, and records how it was queried."""

    person_scope = object()

    def __init__(
        self,
        groups,
        texts,
        *,
        subject_names=("先躺会再说",),
        senders=None,
    ) -> None:
        self._groups = (
            tuple(groups.items()) if isinstance(groups, dict) else tuple(groups)
        )
        self._texts = dict(texts)
        self._subject_names = tuple(subject_names)
        self._senders = dict(senders or {})
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def resolve_scope(
        self,
        scope,
        *,
        owner_user_id="",
        request_id="",
        trace_id="",
        organization_id=None,
    ):
        ids = tuple(
            dict.fromkeys(value for _, values in self._groups for value in values)
        )
        return ScopeResolution(
            scope=scope,
            resource_ids=ids,
            groups=self._groups,
            anchors=self._subject_names,
            subject_names=self._subject_names,
        )

    def search(self, query: str, resource_ids=(), *, top_k=None):
        self.calls.append((query, tuple(resource_ids)))
        return [
            RetrievedChunk(
                text=self._texts[rid],
                resource_id=rid,
                sender_name=self._senders.get(
                    rid, self._subject_names[0] if self._subject_names else ""
                ),
            )
            for rid in resource_ids
            if rid in self._texts
        ]


def _preview(retriever, request: str, headers: list[str] | None = None):
    from tests.test_form_capabilities import FakeClient
    from app.capabilities.form import FormPreviewCapability

    url = "https://example.com/form"
    client = FakeClient()
    client.grid = {"headers": headers or ["学号"], "rows": []}
    capability = FormPreviewCapability(client, retriever=retriever)
    return capability.execute(capability.validate({"request": request, "url": url}))


def test_his_own_message_confirms_the_value_without_repeating_his_name() -> None:
    retriever = _GroupedRetriever({"sent": ("res-1",)}, {"res-1": "学号: 20251714205"})

    result = _preview(retriever, "把先躺会再说的信息填进 https://example.com/form")
    field = result["form"]["fields"][0]

    assert field["value"] == "20251714205"
    assert field["tier"] == "a"
    # His own messages rarely repeat his name, so the query must not carry it.
    assert retriever.calls == [("学号", ("res-1",))]


def test_a_mention_is_not_used_to_fill_a_person_field() -> None:
    retriever = _GroupedRetriever(
        {"mention": ("res-2",)}, {"res-2": "先躺会再说 学号 20251714205"}
    )

    result = _preview(retriever, "把先躺会再说的信息填进 https://example.com/form")
    field = result["form"]["fields"][0]

    assert field["value"] == ""
    assert field["tier"] == "none"
    assert any("本人发出" in warning for warning in result["warnings"])
    assert retriever.calls == []


def test_a_mention_without_the_name_is_not_used() -> None:
    retriever = _GroupedRetriever(
        {"mention": ("res-2",)}, {"res-2": "学号 20251714205"}
    )

    result = _preview(retriever, "把先躺会再说的信息填进 https://example.com/form")

    assert result["form"]["fields"][0]["tier"] == "none"
    assert result["form"]["fields"][0]["value"] == ""


def test_the_other_sides_words_in_a_private_chat_are_not_used() -> None:
    """Only the target's own line can fill the target's fields."""

    retriever = _GroupedRetriever(
        {"private": ("res-2",)},
        {"res-2": "学号 20251714205"},
        subject_names=("先躺会再说",),
        senders={"res-2": "Castorice"},
    )

    result = _preview(retriever, "把先躺会再说的信息填进 https://example.com/form")
    field = result["form"]["fields"][0]

    assert field["value"] == ""
    assert field["tier"] == "none"
    assert any("本人发出" in warning for warning in result["warnings"])
    assert retriever.calls == []


def test_his_own_private_message_is_confirmed_through_the_sent_window() -> None:
    retriever = _GroupedRetriever(
        {"sent": ("res-2",)},
        {"res-2": "学号 20251714205"},
        subject_names=("先躺会再说",),
        senders={"res-2": "先躺会再说"},
    )

    result = _preview(retriever, "把先躺会再说的信息填进 https://example.com/form")

    assert result["form"]["fields"][0]["tier"] == "a"


class _ScopeRetriever:
    """Filter-form retriever: hybrid in scope, newest window, neighbours."""

    person_scope = object()

    def __init__(
        self,
        *,
        hybrid=(),
        recent_texts=(),
        context=(),
    ) -> None:
        self._hybrid = tuple(hybrid)
        self._recent_texts = tuple(recent_texts)
        self._context = tuple(context)
        self.scope_calls: list[tuple] = []
        self.recent_calls: list[dict] = []
        self.context_calls: list[tuple] = []

    def resolve_scope(self, scope, **kwargs):
        return ScopeResolution(
            scope=scope,
            resource_ids=("id-1",),
            groups=(("sent", ("id-1",)),),
            anchors=("先躺会再说",),
            subject_names=("先躺会再说",),
        )

    def search_scope(
        self,
        query,
        *,
        sender_ids=(),
        conversation_ids=(),
        content_contains=(),
        top_k=None,
    ):
        self.scope_calls.append(
            (query, tuple(sender_ids), tuple(conversation_ids), tuple(content_contains))
        )
        return list(self._hybrid)

    def recent_scope_resource_ids(self, **filters):
        self.recent_calls.append(filters)
        return [f"recent-{index}" for index, _ in enumerate(self._recent_texts)]

    def search(self, query, resource_ids=(), *, top_k=None):
        return [
            RetrievedChunk(text=text, resource_id=resource_id)
            for resource_id, text in zip(resource_ids, self._recent_texts)
        ]

    def scope_context(self, anchors, **kwargs):
        self.context_calls.append(tuple(anchors))
        return list(self._context)


def test_the_candidate_set_merges_topk_recent_and_context() -> None:
    """The red line: a ranked list alone must not decide the candidate set."""

    retriever = _ScopeRetriever(
        hybrid=(
            RetrievedChunk(
                text="学号: 20251714205",
                resource_id="hit-1",
                conversation_id="conv-1",
                sent_at="2026-10-05T08:12:17Z",
            ),
        ),
        # The newest window holds a bare value the ranking missed entirely.
        recent_texts=("联系电话: 15985517150",),
        # The value below only exists in a neighbour of a hit.
        context=(
            RetrievedChunk(
                text="家庭住址: 狗熊岭",
                resource_id="ctx-1",
                conversation_id="conv-1",
            ),
        ),
    )

    _preview(
        retriever,
        "帮我把我的信息填入 https://example.com/form",
        headers=["学号", "联系电话", "家庭住址"],
    )

    assert retriever.scope_calls, "the hybrid search must run inside the scope"
    assert retriever.recent_calls, "the newest window must be consulted"
    assert retriever.context_calls, "neighbour context must be pulled in"
    # The window is expressed as filters, not as an id list.
    assert retriever.scope_calls[0][1] == ("id-1",)


def test_a_value_only_in_the_context_is_still_used() -> None:
    retriever = _ScopeRetriever(
        hybrid=(
            RetrievedChunk(
                text="学号: 20251714205",
                resource_id="hit-1",
                conversation_id="conv-1",
                sent_at="2026-10-05T08:12:17Z",
            ),
        ),
        context=(
            RetrievedChunk(
                text="联系电话: 15985517150",
                resource_id="ctx-1",
                sender_name="先躺会再说",
                conversation_id="conv-1",
            ),
        ),
    )

    result = _preview(
        retriever,
        "帮我把我的信息填入 https://example.com/form",
        headers=["学号", "联系电话"],
    )

    by_name = {field["name"]: field for field in result["form"]["fields"]}
    assert by_name["联系电话"]["value"] == "15985517150"
    # The neighbour carries the subject's own sender metadata, so it is still
    # his statement rather than the counterparty's.
    assert by_name["联系电话"]["tier"] == "a"


def test_person_form_queries_only_the_subject_sent_window() -> None:
    """Private and mention windows are context, not value-search windows."""

    retriever = _GroupedRetriever(
        {"sent": ("res-1",), "private": ("res-2",), "mention": ("res-3",)},
        {},
    )

    _preview(retriever, "把先躺会再说 的信息填进 https://example.com/form")

    pairs = set(retriever.calls)
    assert ("学号", ("res-1",)) in pairs
    assert ("学号", ("res-2",)) not in pairs
    assert ("先躺会再说 学号", ("res-3",)) not in pairs


def test_the_owner_is_resolved_to_his_own_identity() -> None:
    retriever = _GroupedRetriever({"sent": ("res-9",)}, {"res-9": "学号: 20251714205"})

    result = _preview(retriever, "帮我把我的信息填入 https://example.com/form")
    field = result["form"]["fields"][0]

    assert field["value"] == "20251714205"
    assert field["tier"] == "a"
    assert retriever.calls == [("学号", ("res-9",))]


# ---------------------------------------------------------------------------
# Routing: an organization question is a knowledge question
# ---------------------------------------------------------------------------


class _StubPlanner:
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


def _task(text: str) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="t1",
        source_type="chat",
        owner_user_id="u1",
        input={"text": text},
        created_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
    )


def _verdict(name: str, confidence: float = 0.95) -> TaskUnderstanding:
    return TaskUnderstanding(
        is_task=True,
        goal="goal",
        task_kind="answer",
        intent_candidates=[UnderstandingIntent(name=name, confidence=confidence)],
        confidence=confidence,
    )


_KNOWLEDGE_CAPABILITIES = [
    _descriptor(SEARCH_SOURCES_NAME),
    _descriptor(SEARCH_CONTENT_NAME),
    _descriptor(KNOWLEDGE_ANSWER_NAME),
]


def test_a_company_question_is_answered_from_its_own_material() -> None:
    stub = _StubPlanner(ANSWER_COMPOSE_NAME, {"question": "深空公司的情况是什么"})
    planner = KnowledgeRoutingPlanner(stub)

    plan = planner.create_plan(
        _task("深空公司的情况是什么"),
        [*_KNOWLEDGE_CAPABILITIES, _descriptor(PERSON_QUERY_NAME)],
        [],
        PlanningConstraints(),
        _verdict(PERSON_QUERY_NAME),
    )

    assert [step.capability for step in plan.steps] == [
        SEARCH_SOURCES_NAME,
        SEARCH_CONTENT_NAME,
        KNOWLEDGE_ANSWER_NAME,
    ]
    # The scope is the company, pinned to the full name in the body.
    assert plan.steps[0].arguments["content_contains"] == ["深空公司"]
    # The retrieval is closed over those sources instead of the whole library.
    assert plan.steps[1].arguments["restrict_to_resource_ids"] is True
    assert plan.steps[1].arguments["resource_ids_ref"] == {
        "step": 1,
        "output": "resource_ids",
    }
    # Empty evidence says which company was missing, not a generic line.
    assert "深空公司" in plan.steps[2].arguments["empty_answer"]


def test_a_person_question_still_goes_to_the_person_tool() -> None:
    # The extractors do not read this wording, so the verdict is what routes it.
    stub = _StubPlanner(PERSON_QUERY_NAME, {"name": "张三", "question": "张三现在忙不忙"})
    planner = KnowledgeRoutingPlanner(stub)

    plan = planner.create_plan(
        _task("张三现在忙不忙"),
        [*_KNOWLEDGE_CAPABILITIES, _descriptor(PERSON_QUERY_NAME)],
        [],
        PlanningConstraints(),
        _verdict(PERSON_QUERY_NAME),
    )

    assert [step.capability for step in plan.steps] == [PERSON_QUERY_NAME]
    assert stub.plans == 1


def test_person_facet_questions_use_the_person_tool() -> None:
    planner = KnowledgeRoutingPlanner(
        _StubPlanner(PERSON_QUERY_NAME, {"name": "unused", "question": "unused"})
    )
    capabilities = [
        *_KNOWLEDGE_CAPABILITIES,
        _descriptor(PERSON_QUERY_NAME),
    ]

    project = planner.create_plan(
        _task("我想要知道小超的项目情况"),
        capabilities,
        [],
        PlanningConstraints(),
        _verdict("knowledge.answer"),
    )
    contact = planner.create_plan(
        _task("杨思琪的联系方式"),
        capabilities,
        [],
        PlanningConstraints(),
        _verdict("knowledge.answer"),
    )

    assert [step.capability for step in project.steps] == [PERSON_QUERY_NAME]
    assert project.steps[0].arguments["name"] == "小超"
    assert [step.capability for step in contact.steps] == [PERSON_QUERY_NAME]
    assert contact.steps[0].arguments["name"] == "杨思琪"


def test_the_verdict_cannot_pull_a_company_into_the_person_tool() -> None:
    # The extractors read no name here; only the organization word shows that
    # this is not a person, and the verdict alone must not decide.
    stub = _StubPlanner(PERSON_QUERY_NAME, {"name": "深空公司", "question": "深空公司那边最近怎么样"})
    planner = KnowledgeRoutingPlanner(stub)

    plan = planner.create_plan(
        _task("深空公司那边最近怎么样"),
        [*_KNOWLEDGE_CAPABILITIES, _descriptor(PERSON_QUERY_NAME)],
        [],
        PlanningConstraints(),
        _verdict(PERSON_QUERY_NAME),
    )

    assert [step.capability for step in plan.steps] == [
        SEARCH_SOURCES_NAME,
        SEARCH_CONTENT_NAME,
        KNOWLEDGE_ANSWER_NAME,
    ]
    assert stub.plans == 0


def test_a_project_question_keeps_the_plain_knowledge_route() -> None:
    # The classifier may still name a person when the sentence starts with one.
    # The question is about the work, and his private window is the wrong place
    # to answer it from, so the person route is skipped.
    stub = _StubPlanner(
        PERSON_QUERY_NAME, {"name": "张三", "question": "张三最近关于项目有什么进展吗？"}
    )
    planner = KnowledgeRoutingPlanner(stub)

    plan = planner.create_plan(
        _task("张三最近关于项目有什么进展吗？"),
        [*_KNOWLEDGE_CAPABILITIES, _descriptor(PERSON_QUERY_NAME)],
        [],
        PlanningConstraints(),
        _verdict(PERSON_QUERY_NAME),
    )

    assert stub.plans == 0
    assert PERSON_QUERY_NAME not in [step.capability for step in plan.steps]
    capabilities_used = [step.capability for step in plan.steps]
    assert SEARCH_CONTENT_NAME in capabilities_used
    assert KNOWLEDGE_ANSWER_NAME in capabilities_used
