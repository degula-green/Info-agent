from __future__ import annotations

from typing import Any

from app.capabilities.person import PersonQueryCapability, PersonQueryInput
from app.capabilities.person_scope import mention_anchors, usable_person_name
from app.infrastructure.knowledge.client import KnowledgeUnavailable
from app.kernel.execution_context import ExecutionContext, bind_execution_context
from app.planning.knowledge import classify_person_question
from app.providers.person_facts import PersonFact
from app.testing.in_memory_runtime_store import InMemoryAgentStore


def _context() -> ExecutionContext:
    return ExecutionContext(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        owner_user_id="user-1",
        organization_id="org-1",
        request_id="request-1",
        trace_id="trace-1",
        source_type="chat",
        source_ref={"organization_id": "org-1"},
    )


class _RAG:
    def __init__(self) -> None:
        self.source_calls: list[dict[str, Any]] = []
        self.content_calls: list[dict[str, Any]] = []

    def search_sources(self, body, **identity):
        self.source_calls.append(dict(body))
        return {
            "items": [],
            "resource_ids": ["resource-1"],
            "returned_count": 1,
            "has_more": False,
            "diagnostics": {"metadata_coverage": "complete"},
        }

    def export_scope(self, body, **identity):
        """The complete-scope primitive the person window pages through."""

        self.source_calls.append(dict(body))
        return {
            "items": [
                {
                    "chunk_id": "chunk-1",
                    "resource_id": "resource-1",
                    "resource_type": "message",
                    "title": "会话",
                    "sender": {"id": "id-1", "name": "张三"},
                    "conversation": {"id": "conversation-1", "name": "项目群"},
                    "sent_at": "2026-10-01T10:30:00+08:00",
                    "text": "我已经完成了官网部署",
                }
            ],
            "next_offset": 1,
            "has_more": False,
            "diagnostics": {"metadata_coverage": "complete"},
        }

    def search_content(self, body, **identity):
        self.content_calls.append(dict(body))
        return {
            "items": [
                {
                    "resource_id": "resource-1",
                    "resource_type": "message",
                    "title": "会话",
                    "sender": {"name": "张三"},
                    "conversation": {
                        "id": "conversation-1",
                        "name": "项目群",
                        "type": "group",
                        "platform": "feishu",
                    },
                    "sent_at": "2026-10-01T10:30:00+08:00",
                    "best_score": 0.5,
                    "chunks": [
                        {
                            "chunk_id": "chunk-1",
                            "text": "张三负责部署",
                            "score": 0.5,
                            "score_type": "rrf",
                        }
                    ],
                }
            ],
            "returned_source_count": 1,
            "returned_chunk_count": 1,
            "has_more": False,
            "diagnostics": {"metadata_coverage": "complete"},
        }


class _Knowledge:
    def __init__(self, matches: list[dict[str, Any]]) -> None:
        self.matches = matches
        self.calls: list[dict[str, Any]] = []

    def resolve_person(self, **kwargs):
        self.calls.append(kwargs)
        return {"subject_name": "小李", "matches": self.matches}


class _UnavailableKnowledge:
    def resolve_person(self, **kwargs):
        raise KnowledgeUnavailable("knowledge unavailable")


class _Provider:
    def compose(self, question, evidence, **kwargs):
        return type(
            "Draft",
            (),
            {
                "answer": "小李负责部署",
                "citations": [
                    {
                        "evidence_id": evidence[0]["evidence_id"],
                        "quote": evidence[0]["snippet"],
                    }
                ],
                "model_calls": 1,
            },
        )()


def _match(**overrides):
    value = {
        "person_key": "person-1",
        "display_name": "小李",
        "attached": True,
        "identity_ids": ["identity-1"],
        "private_conversation_ids": ["conversation-private"],
    }
    value.update(overrides)
    return value


def test_classify_person_question_extracts_name() -> None:
    assert classify_person_question("张三负责的部署现在怎么样了") == (
        "张三",
        "张三负责的部署现在怎么样了",
    )
    assert classify_person_question("问一下王五的情况") == (
        "王五",
        "问一下王五的情况",
    )
    assert classify_person_question("青云官网是什么") is None
    assert classify_person_question("张三发过哪些文件") is None


def test_person_query_falls_back_to_content_when_person_lookup_is_unavailable() -> None:
    rag = _RAG()
    capability = PersonQueryCapability(rag, _UnavailableKnowledge(), _Provider())

    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小李", question="小李最近的任务是什么")
        )

    assert output["coverage"]["path"] == "generic_fallback"
    assert rag.content_calls


def test_classify_person_question_accepts_who_is_and_profile_intro() -> None:
    assert classify_person_question("小奥子是谁，给我介绍一下") == (
        "小奥子",
        "小奥子是谁，给我介绍一下",
    )
    assert classify_person_question("我想要知道小呆呆仓鼠的个人情况，帮我介绍一下") == (
        "小呆呆仓鼠",
        "我想要知道小呆呆仓鼠的个人情况，帮我介绍一下",
    )
    assert classify_person_question("我想要知道小呆呆仓鼠关于项目工作的情况") == (
        "小呆呆仓鼠",
        "我想要知道小呆呆仓鼠关于项目工作的情况",
    )
    assert classify_person_question("我想要知道小超的项目情况") == (
        "小超",
        "我想要知道小超的项目情况",
    )
    assert classify_person_question("杨思琪的联系方式") == (
        "杨思琪",
        "杨思琪的联系方式",
    )


class _RecordingProvider:
    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []
        self.sections: dict[str, list[dict[str, Any]]] = {}
        self.question = ""
        self.last_call_count = 0

    def compose(self, question, evidence, **kwargs):
        self.question = str(question or "")
        self.evidence = list(evidence)
        self.last_call_count = 1
        return type(
            "Draft",
            (),
            {"answer": "ok", "citations": [], "model_calls": 1},
        )()

    def compose_person_sections(self, question, sections, **kwargs):
        self.question = str(question or "")
        self.conversation_excerpts = list(
            kwargs.get("conversation_excerpts") or []
        )
        self.sections = {
            str(key): [dict(item) for item in items]
            for key, items in sections.items()
        }
        self.evidence = []
        for items in self.sections.values():
            for item in items:
                converted = dict(item)
                converted.setdefault("fact_value", item.get("value"))
                converted.setdefault("fact_category", item.get("category"))
                self.evidence.append(converted)
        self.last_call_count = 1
        summaries: dict[str, str] = {}
        if self.conversation_excerpts:
            summaries["recent_activity"] = "近期对话已归纳。"
        return summaries


class _CachedKnowledge(_Knowledge):
    def __init__(self, matches: list[dict[str, Any]]) -> None:
        super().__init__(matches)
        self.profile_calls: list[dict[str, Any]] = []

    def person_profile(self, **kwargs):
        self.profile_calls.append(kwargs)
        return {
            "display_name": "Castorice",
            "profile": {"summary": "是谁：Castorice，学生，学号20251714257"},
            "facts": [
                {
                    "id": "fact-phone",
                    "fact_type": "phone",
                    "label": "手机号",
                    "raw_value": "15325865236",
                    "occurred_at": "2026-10-05T09:31:11+00:00",
                }
            ],
        }


class _ExportRAG:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def export_scope(self, body, **identity):
        self.calls.append(dict(body))
        if body.get("resource_types") != ["message"]:
            return {"items": [], "has_more": False, "next_offset": 0}
        if body.get("sender_ids"):
            return {
                "items": [
                    {
                        "chunk_id": "chunk-self",
                        "resource_id": "resource-self",
                        "resource_type": "message",
                        "sender": {"id": "identity-1", "name": "Castorice"},
                        "conversation": {"id": "conv-1", "name": "Castorice"},
                        "sent_at": "2026-10-05T09:31:11+00:00",
                        "text": "手机号15325865236，邮箱2865346529@qq.com",
                    },
                    {
                        "chunk_id": "chunk-project",
                        "resource_id": "resource-project",
                        "resource_type": "message",
                        "sender": {"id": "identity-1", "name": "Castorice"},
                        "conversation": {"id": "conv-1", "name": "Castorice"},
                        "sent_at": "2026-10-08T02:05:52+00:00",
                        "text": "完成采集表单配置模块，支持文本、单选、多选、附件4类字段，已联调通过",
                    },
                ],
                "has_more": False,
                "next_offset": 2,
            }
        if body.get("conversation_ids"):
            return {
                "items": [
                    {
                        "chunk_id": "chunk-other",
                        "resource_id": "resource-other",
                        "resource_type": "message",
                        "sender": {"id": "identity-owner", "name": "稻成"},
                        "conversation": {"id": "conv-1", "name": "Castorice"},
                        "sent_at": "2026-10-05T09:32:00+00:00",
                        "text": "他说他最近在准备PPT",
                    }
                ],
                "has_more": False,
                "next_offset": 1,
            }
        return {"items": [], "has_more": False, "next_offset": 0}


class _FactExtractor:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def extract(self, *, subject, aliases, question, messages):
        self.batches.append(list(messages))
        return [
            PersonFact(
                fact_type="phone",
                label="手机号",
                value="15325865236",
                quote="手机号15325865236，邮箱2865346529@qq.com",
                speaker_role="subject",
                resource_id="resource-self",
                sent_at="2026-10-05T09:31:11+00:00",
            )
        ]


class _ProjectFactExtractor:
    def extract(self, *, subject, aliases, question, messages):
        return [
            PersonFact(
                fact_type="project",
                label="项目工作",
                value="完成采集表单配置模块",
                quote="完成采集表单配置模块",
                speaker_role="subject",
                resource_id="resource-project",
                sent_at="2026-10-08T02:05:52+00:00",
            )
        ]


class _AliasRecordingExtractor:
    def __init__(self) -> None:
        self.aliases: list[str] = []
        self.questions: list[str] = []
        self.calls = 0

    def extract(self, *, subject, aliases, question, messages):
        self.calls += 1
        self.aliases = list(aliases)
        self.questions.append(question)
        return [
            PersonFact(
                fact_type="phone",
                label="手机号",
                value="15325865236",
                quote="手机号15325865236，邮箱2865346529@qq.com",
                speaker_role="subject",
                resource_id="resource-self",
                sent_at="2026-10-05T09:31:11+00:00",
            )
        ]


class _HallucinatingExtractor:
    def extract(self, *, subject, aliases, question, messages):
        return [
            PersonFact(
                fact_type="age",
                label="年龄",
                value="46岁",
                quote="今年46岁",
                speaker_role="subject",
                resource_id="resource-self",
            )
        ]


class _OtherFactExtractor:
    def extract(self, *, subject, aliases, question, messages):
        return [
            PersonFact(
                fact_type="event",
                label="近期活动",
                value="PPT",
                quote="他说他最近在准备PPT",
                speaker_role="other",
                resource_id="resource-other",
            )
        ]


def test_person_query_prefers_person_profile_cache() -> None:
    provider = _RecordingProvider()
    knowledge = _CachedKnowledge([_match(relation_ids=["relation-1"])])
    capability = PersonQueryCapability(_RAG(), knowledge, provider)
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小呆呆仓鼠", question="小呆呆仓鼠的手机号是多少")
        )
    assert output["coverage"]["path"] == "cache"
    assert knowledge.profile_calls
    assert any(
        item.get("fact_value") == "15325865236" for item in provider.evidence
    )
    assert all(
        item.get("attribution") != "profile" for item in provider.evidence
    )


def test_person_query_overview_merges_cache_and_project_facts() -> None:
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _CachedKnowledge(
            [
                _match(
                    relation_ids=["relation-1"],
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                )
            ]
        ),
        provider,
        fact_extractor=_ProjectFactExtractor(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小呆呆仓鼠", question="我想要知道小呆呆仓鼠的情况")
        )
    assert output["coverage"]["path"] == "cache+full_scope_extraction"
    assert any(
        item.get("attribution") == "profile" for item in provider.evidence
    )
    assert any(
        item.get("fact_value") == "完成采集表单配置模块"
        for item in provider.evidence
    )


def test_person_query_collects_platform_aliases() -> None:
    capability = PersonQueryCapability(_RAG(), _Knowledge([_match()]), _Provider())
    match = _match(
        identities=[
            {
                "display_name": "Andrea",
                "external_user_id": "wxid_qm94s33mq2au22",
                "platform": "wechat",
            }
        ]
    )
    assert capability._person_aliases(match, "杨思琪", "杨思琪") == [
        "杨思琪",
        "Andrea",
        "wxid_qm94s33mq2au22",
    ]


def test_person_query_passes_platform_aliases_to_extractor() -> None:
    extractor = _AliasRecordingExtractor()
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _Knowledge(
            [
                _match(
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                    display_name="小超",
                    identities=[
                        {
                            "display_name": "德古拉green",
                            "external_user_id": "wxid_h4jqziig5yyt22",
                            "platform": "wechat",
                        }
                    ],
                )
            ]
        ),
        provider,
        fact_extractor=extractor,
    )
    with bind_execution_context(_context()):
        capability.execute(PersonQueryInput(name="小超", question="我想要知道小超的情况"))
    assert "德古拉green" in extractor.aliases
    assert "德古拉green" in provider.question


def test_person_query_reuses_fact_snapshot() -> None:
    store = InMemoryAgentStore()
    extractor = _AliasRecordingExtractor()
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _Knowledge(
            [
                _match(
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                    display_name="小超",
                    identities=[
                        {
                            "display_name": "德古拉green",
                            "external_user_id": "wxid_h4jqziig5yyt22",
                            "platform": "wechat",
                        }
                    ],
                )
            ]
        ),
        provider,
        fact_extractor=extractor,
        fact_store=store,
    )
    with bind_execution_context(_context()):
        first = capability.execute(
            PersonQueryInput(name="小超", question="我想要知道小超的情况")
        )
        second = capability.execute(
            PersonQueryInput(name="小超", question="我想要知道小超的情况")
        )
    assert extractor.calls == 1
    assert first["coverage"]["snapshot_hit"] is False
    assert second["coverage"]["snapshot_hit"] is True
    assert second["coverage"]["facts"] == first["coverage"]["facts"]


def test_person_query_exports_full_scope_and_attributes_speaker() -> None:
    rag = _ExportRAG()
    provider = _RecordingProvider()
    extractor = _FactExtractor()
    capability = PersonQueryCapability(
        rag,
        _Knowledge([_match(identity_ids=["identity-1"], private_conversation_ids=["conv-1"])]),
        provider,
        fact_extractor=extractor,
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小呆呆仓鼠", question="介绍一下小呆呆仓鼠")
        )
    assert output["coverage"]["path"] == "full_scope_extraction"
    assert extractor.batches
    assert any(
        item.get("attribution") == "subject_said"
        and item.get("fact_value") == "15325865236"
        for item in provider.evidence
    )


def test_person_query_drops_ungrounded_fact() -> None:
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _Knowledge([_match(identity_ids=["identity-1"], private_conversation_ids=["conv-1"])]),
        provider,
        fact_extractor=_HallucinatingExtractor(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小呆呆仓鼠", question="介绍一下小呆呆仓鼠")
        )
    assert output["coverage"]["facts"] == 0
    assert not any(
        item.get("fact_value") == "46岁" for item in provider.evidence
    )
    assert output["answer"].startswith("## 近期活动")
    assert "## 身份" not in output["answer"]


def test_person_query_drops_other_speaker_fact_without_subject_name() -> None:
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _Knowledge([_match(identity_ids=["identity-1"], private_conversation_ids=["conv-1"])]),
        provider,
        fact_extractor=_OtherFactExtractor(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小呆呆仓鼠", question="介绍一下小呆呆仓鼠")
        )
    assert output["coverage"]["facts"] == 0
    assert not any(
        item.get("fact_value") == "PPT" for item in provider.evidence
    )


def test_person_query_ignores_raw_wechat_payloads() -> None:
    capability = PersonQueryCapability(_RAG(), _Knowledge([_match()]), _Provider())
    assert capability._is_raw_payload(
        '<msg><emoji fromusername="wxid_a" tousername="wxid_b" len="46" /></msg>'
    )
    assert capability._is_raw_payload(
        '<msg><appmsg><title>稻成的聊天记录</title></appmsg></msg>'
    )
    assert not capability._is_raw_payload("手机号15325865236，邮箱2865346529@qq.com")


def test_person_query_merges_three_sources_and_answers() -> None:
    rag = _RAG()
    capability = PersonQueryCapability(rag, _Knowledge([_match()]), _Provider())
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="暴躁小李", question="小李负责的部署怎么样")
        )
    assert output["answer"] == "小李负责部署"
    assert output["citations"]
    assert any("sender_ids" in call for call in rag.source_calls)
    assert any("conversation_ids" in call for call in rag.source_calls)
    assert any("content_contains" in call for call in rag.source_calls)
    assert rag.content_calls
    assert rag.content_calls[0]["restrict_to_resource_ids"] is True


def test_person_query_asks_for_disambiguation() -> None:
    capability = PersonQueryCapability(
        _RAG(),
        _Knowledge([_match(person_key="a", display_name="小李"), _match(person_key="b", display_name="小李")]),
        _Provider(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(PersonQueryInput(name="小李", question="小李的情况"))
    assert output["needs_disambiguation"] is True
    assert len(output["candidates"]) == 2


def test_facet_extraction_still_builds_complete_snapshot() -> None:
    store = InMemoryAgentStore()
    extractor = _AliasRecordingExtractor()
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _Knowledge(
            [
                _match(
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                )
            ]
        ),
        provider,
        fact_extractor=extractor,
        fact_store=store,
    )
    with bind_execution_context(_context()):
        facet = capability.execute(
            PersonQueryInput(name="小超", question="小超的项目情况")
        )
        overview = capability.execute(
            PersonQueryInput(name="小超", question="小超的情况")
        )

    assert facet["coverage"]["path"] == "facet_no_facts"
    assert extractor.calls == 1
    assert "所有类别" in extractor.questions[0]
    assert overview["coverage"]["snapshot_hit"] is True
    assert any(
        item.get("fact_value") == "15325865236"
        for item in provider.evidence
    )


def test_facet_uses_extraction_instead_of_unrelated_profile_cache() -> None:
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _CachedKnowledge(
            [
                _match(
                    relation_ids=["relation-1"],
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                )
            ]
        ),
        provider,
        fact_extractor=_ProjectFactExtractor(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小超", question="小超的项目情况")
        )

    assert output["coverage"]["path"] == "full_scope_extraction"
    assert provider.evidence
    assert all(
        item.get("fact_category") == "project"
        for item in provider.evidence
    )
    assert not any(
        item.get("fact_value") == "15325865236"
        for item in provider.evidence
    )


def test_person_query_uses_platform_names_for_mentions() -> None:
    rag = _ExportRAG()
    capability = PersonQueryCapability(
        rag,
        _Knowledge(
            [
                _match(
                    person_key="person-1",
                    display_name="小超",
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                    identities=[
                        {
                            "display_name": "德古拉green",
                            "external_user_id": "wxid_h4jqziig5yyt22",
                            "platform": "wechat",
                        }
                    ],
                )
            ]
        ),
        _RecordingProvider(),
        fact_extractor=_ProjectFactExtractor(),
    )
    with bind_execution_context(_context()):
        capability.execute(
            PersonQueryInput(name="小超", question="小超的情况")
        )

    mention_calls = [
        call for call in rag.calls if call.get("content_contains")
    ]
    assert mention_calls
    assert all(call["content_contains"] == ["德古拉green"] for call in mention_calls)
    assert all("小超" not in call["content_contains"] for call in mention_calls)


def test_self_pronoun_is_not_a_mention_anchor() -> None:
    assert not usable_person_name("我")
    assert not usable_person_name("我的")
    assert usable_person_name("稻成")
    assert mention_anchors(
        {
            "display_name": "稻成",
            "identities": [
                {"display_name": "稻成", "platform": "wechat"},
                {"display_name": "DC", "platform": "feishu"},
            ],
        },
        fallback="我",
    ) == ("稻成", "DC")


def test_self_private_scope_is_bounded_to_recent_twenty_days() -> None:
    rag = _ExportRAG()
    capability = PersonQueryCapability(
        rag,
        _Knowledge(
            [
                _match(
                    person_key="user-1",
                    display_name="稻成",
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                    identities=[
                        {"display_name": "稻成", "platform": "wechat"},
                    ],
                )
            ]
        ),
        _RecordingProvider(),
        fact_extractor=_ProjectFactExtractor(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="我", question="我的情况")
        )

    private_calls = [
        call for call in rag.calls if call.get("conversation_ids") == ["conv-1"]
    ]
    assert private_calls
    assert all(call.get("occurred_after") for call in private_calls)
    assert output["coverage"]["private_occurred_after"]


def test_facet_answer_renders_only_the_requested_section() -> None:
    provider = _RecordingProvider()
    capability = PersonQueryCapability(
        _ExportRAG(),
        _Knowledge(
            [
                _match(
                    identity_ids=["identity-1"],
                    private_conversation_ids=["conv-1"],
                )
            ]
        ),
        provider,
        fact_extractor=_AliasRecordingExtractor(),
    )
    with bind_execution_context(_context()):
        output = capability.execute(
            PersonQueryInput(name="小超", question="小超的联系方式")
        )

    assert output["answer"].startswith("## 联系方式")
    assert "## 身份" not in output["answer"]
    assert "## 工作/项目" not in output["answer"]
    assert "15325865236" in output["answer"]
