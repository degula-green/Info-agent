from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.capabilities.answer import AnswerComposeCapability
from app.capabilities.knowledge import (
    ContentChunk,
    ContentResult,
    KnowledgeAnswerCapability,
    KnowledgeAnswerInput,
    KnowledgeSearchContentCapability,
    KnowledgeSearchSourcesCapability,
    KnowledgeSearchTreeCapability,
    KnowledgeToolUnavailable,
    SearchContentInput,
    SearchSourcesInput,
    SearchTreeInput,
)
from app.capabilities.web_research import WebResearchCapability
from app.container import build_container, build_planner, build_registry
from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
    TaskUnderstanding,
    UnderstandingIntent,
)
from app.kernel.registry import CapabilityRegistry
from app.planning.knowledge import (
    KnowledgeRoutingPlanner,
    build_compliance_plan,
    build_form_plan,
    build_knowledge_plan,
    classify_knowledge_question,
)
from app.policy.descriptor import DescriptorPolicy
from app.testing.fake_publisher import FakeTaskPublisher
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from app.testing.in_memory_todo_store import InMemoryTodoStore
from tests.support import make_settings
from app.kernel.execution_context import (
    ExecutionContext,
    bind_execution_context,
    current_execution_context,
)
from app.kernel.bindings import bind_plan_references
from app.kernel.models import Plan, PlanStep
from app.planning.deterministic import DeterministicPlanner


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


def context(*, organization_id: str | None = "org-1") -> ExecutionContext:
    return ExecutionContext(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        owner_user_id="user-1",
        organization_id=organization_id,
        request_id="request-1",
        trace_id="trace-1",
        source_type="chat",
        source_ref={"organization_id": organization_id} if organization_id else {},
    )


class _RAG:
    def __init__(self) -> None:
        self.source_calls: list[dict[str, Any]] = []
        self.content_calls: list[dict[str, Any]] = []
        self.tree_calls: list[dict[str, Any]] = []
        self.tree_empty = False
        self.content_has_more = False

    @staticmethod
    def _source(resource_id: str, score: float, name: str) -> dict[str, Any]:
        return {
            "resource_id": resource_id,
            "resource_type": "message",
            "knowledge_item_id": f"item-{resource_id}",
            "title": name,
            "sender": {"id": "sender-1", "name": "张三", "platform": "feishu"},
            "conversation": {
                "id": "conversation-1",
                "name": "财务群",
                "type": "group",
                "platform": "feishu",
            },
            "sent_at": "2026-10-01T10:30:00+08:00",
            "score": score,
            "score_type": "rrf",
            "preview": name,
        }

    def search_sources(self, body, **identity):
        self.source_calls.append(dict(body))
        if body["scope_type"] == "organization":
            items = [self._source("resource-1", 0.1, "组织结果")]
        else:
            items = [
                self._source("resource-1", 0.2, "个人结果"),
                self._source("resource-2", 0.15, "个人独有"),
            ]
        return {
            "items": items,
            "resource_ids": [item["resource_id"] for item in items],
            "returned_count": len(items),
            "has_more": False,
            "diagnostics": {"metadata_coverage": "complete"},
        }

    def search_content(self, body, **identity):
        self.content_calls.append(dict(body))
        if body.get("restrict_to_resource_ids") and not body.get("resource_ids"):
            return {
                "items": [],
                "returned_source_count": 0,
                "returned_chunk_count": 0,
                "has_more": False,
                "diagnostics": {"metadata_coverage": "complete"},
            }
        return {
            "items": [
                {
                    "resource_id": "resource-1",
                    "resource_type": "attachment",
                    "title": "预算表.xlsx",
                    "sender": {"name": "张三"},
                    "conversation": {
                        "id": "conversation-1",
                        "name": "财务群",
                        "type": "group",
                        "platform": "feishu",
                    },
                    "sent_at": "2026-10-01T10:30:00+08:00",
                    "best_score": 0.2,
                    "chunks": [
                        {
                            "chunk_id": "chunk-1",
                            "text": "预算内容",
                            "score": 0.2,
                            "score_type": "rrf",
                            "position": {"paragraph_index": 2},
                        }
                    ],
                }
            ],
            "returned_source_count": 1,
            "returned_chunk_count": 1,
            "has_more": self.content_has_more,
            "diagnostics": {"metadata_coverage": "complete"},
        }

    def search_tree(self, body, **identity):
        self.tree_calls.append(dict(body))
        diagnostics = {
            "tree_mode": "tree",
            "effective_execution_path": "tree",
            "fallback_reason": "no_entity_match" if self.tree_empty else None,
            "entity_scope": {
                "entity_ids": [] if self.tree_empty else ["entity-1"],
            },
            "locate_ms": 12.5,
            "metadata_coverage": "complete",
        }
        if self.tree_empty:
            return {"items": [], "has_more": False, "diagnostics": diagnostics}
        return {
            "items": [
                {
                    "chunk_id": "tree-chunk-1",
                    "resource_id": "resource-tree-1",
                    "resource_type": "message",
                    "title": "实体范围内的消息",
                    "source_conversation_id": "conversation-1",
                    "source_conversation_name": "aims",
                    "source_conversation_type": "group",
                    "source_platform": "feishu",
                    "sender_display_name": "张三",
                    "sent_at": "2026-10-01T10:30:00+08:00",
                    "content": "树检索到的内容",
                    "score": 0.4,
                    "score_type": "rrf",
                    "source_locator": {"paragraph_index": 1},
                }
            ],
            "has_more": False,
            "diagnostics": diagnostics,
        }


class _AnswerProvider:
    def __init__(self, *, citations: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.citations = citations

    def compose(self, question: str, evidence: list[dict[str, Any]]):
        self.calls.append((question, [dict(item) for item in evidence]))
        citations = self.citations
        if citations is None:
            citations = [
                {"evidence_id": item["evidence_id"], "quote": item["snippet"]}
                for item in evidence
            ]
        return type(
            "Draft",
            (),
            {
                "answer": "检索到的回答",
                "citations": citations,
                "model_calls": 1,
            },
        )()


def test_execution_context_is_isolated_and_reset() -> None:
    with bind_execution_context(context()):
        assert current_execution_context().owner_user_id == "user-1"

    with pytest.raises(RuntimeError):
        current_execution_context()

    def owner(value: str) -> str:
        ctx = context()
        object.__setattr__(ctx, "owner_user_id", value)
        with bind_execution_context(ctx):
            return current_execution_context().owner_user_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(owner, ["user-a", "user-b"])) == ["user-a", "user-b"]


def test_source_capability_merges_organization_and_personal() -> None:
    client = _RAG()
    capability = KnowledgeSearchSourcesCapability(client)
    with bind_execution_context(context()):
        output = capability.execute(
            SearchSourcesInput(
                query="预算",
                include_personal=True,
                top_k=10,
            )
        )

    assert output["resource_ids"] == ["resource-1", "resource-2"]
    assert output["returned_count"] == 2
    assert {call["scope_type"] for call in client.source_calls} == {
        "organization",
        "user",
    }
    assert all("user_id" not in call for call in client.source_calls)


def test_source_capability_fails_closed_without_organization() -> None:
    capability = KnowledgeSearchSourcesCapability(_RAG())
    with bind_execution_context(context(organization_id=None)):
        with pytest.raises(KnowledgeToolUnavailable):
            capability.execute(SearchSourcesInput(query="预算"))


def test_tool_inputs_reject_identity_fields() -> None:
    with pytest.raises(ValidationError):
        SearchSourcesInput.model_validate({"query": "预算", "user_id": "other"})
    with pytest.raises(ValidationError):
        SearchContentInput.model_validate(
            {
                "query": "预算",
                "resource_ids": ["resource-1"],
                "organization_id": "other",
            }
        )
    with pytest.raises(ValidationError):
        SearchTreeInput.model_validate(
            {"query": "预算", "user_id": "other"}
        )


def test_content_capability_declares_resource_ids_binding() -> None:
    descriptor = KnowledgeSearchContentCapability.descriptor
    assert descriptor.input_bindings[0].planner_argument == "resource_ids_ref"
    assert descriptor.input_bindings[0].runtime_argument == "resource_ids"
    assert descriptor.input_bindings[0].source_output == "resource_ids"


def test_tree_capability_returns_entity_scoped_results() -> None:
    client = _RAG()
    content = KnowledgeSearchContentCapability(client)
    capability = KnowledgeSearchTreeCapability(client, content)
    with bind_execution_context(context()):
        output = capability.execute(SearchTreeInput(query="aims 新版app"))

    assert output["retrieval_path"] == "tree"
    assert output["tree_mode"] == "tree"
    assert output["entity_ids"] == ["entity-1"]
    assert output["locate_ms"] == 12.5
    assert output["returned_source_count"] == 1
    assert output["evidence"][0]["quote"] == "树检索到的内容"
    assert client.content_calls == []


def test_tree_capability_falls_back_to_traditional_content() -> None:
    client = _RAG()
    client.tree_empty = True
    client.content_has_more = True
    content = KnowledgeSearchContentCapability(client)
    capability = KnowledgeSearchTreeCapability(client, content)
    with bind_execution_context(context()):
        output = capability.execute(
            SearchTreeInput(query="新版app什么时候发布", fallback_to_content=True)
        )

    assert output["retrieval_path"] == "traditional_fallback"
    assert output["fallback_reason"] == "no_entity_match"
    assert output["has_more"] is True
    assert output["returned_source_count"] == 1
    assert output["evidence"][0]["quote"] == "预算内容"
    assert len(client.tree_calls) == 1
    assert len(client.content_calls) == 1


def test_tree_capability_can_disable_fallback() -> None:
    client = _RAG()
    client.tree_empty = True
    content = KnowledgeSearchContentCapability(client)
    capability = KnowledgeSearchTreeCapability(client, content)
    with bind_execution_context(context()):
        output = capability.execute(
            SearchTreeInput(query="新版app什么时候发布", fallback_to_content=False)
        )

    assert output["retrieval_path"] == "tree"
    assert output["returned_source_count"] == 0
    assert output["fallback_reason"] == "no_entity_match"
    assert client.content_calls == []


def test_search_sources_output_binds_into_search_content() -> None:
    descriptors = [
        KnowledgeSearchSourcesCapability.descriptor,
        KnowledgeSearchContentCapability.descriptor,
    ]
    plan = Plan(
        plan_id="plan-1",
        task_id="task-1",
        objective="先定位再检索",
        steps=[
            PlanStep(
                step_id="step-1",
                plan_id="plan-1",
                order=1,
                capability="knowledge.search_sources",
                arguments={"query": "预算"},
            ),
            PlanStep(
                step_id="step-2",
                plan_id="plan-1",
                order=2,
                capability="knowledge.search_content",
                arguments={
                    "query": "违约条款",
                    "resource_ids_ref": {"step": 1, "output": "resource_ids"},
                },
            ),
        ],
    )

    bound = bind_plan_references(plan, descriptors)
    assert "resource_ids_ref" not in bound.steps[1].arguments
    assert (
        bound.steps[1].arguments["resource_ids"]
        == "$steps.step-1.output.resource_ids"
    )


def test_tree_results_bind_into_knowledge_answer() -> None:
    descriptors = [
        KnowledgeSearchTreeCapability.descriptor,
        KnowledgeAnswerCapability.descriptor,
    ]
    plan = Plan(
        plan_id="plan-1",
        task_id="task-1",
        objective="树检索后回答",
        steps=[
            PlanStep(
                step_id="step-1",
                plan_id="plan-1",
                order=1,
                capability="knowledge.search_tree",
                arguments={"query": "青云飞鹏交付"},
            ),
            PlanStep(
                step_id="step-2",
                plan_id="plan-1",
                order=2,
                capability="knowledge.answer",
                arguments={
                    "query": "青云飞鹏交付",
                    "results_ref": {"step": 1, "output": "results"},
                },
            ),
        ],
    )

    bound = bind_plan_references(plan, descriptors)

    assert "results_ref" not in bound.steps[1].arguments
    assert (
        bound.steps[1].arguments["results"]
        == "$steps.step-1.output.results"
    )


def test_answer_citations_group_chunks_from_one_resource() -> None:
    capability = KnowledgeAnswerCapability(_AnswerProvider())
    output = capability.execute(
        KnowledgeAnswerInput(
            query="青云飞鹏官网在讲什么",
            results=[
                ContentResult(
                    resource_id="attachment-1",
                    resource_type="attachment",
                    title="青云飞鹏",
                    best_score=0.9,
                    matched_chunk_count=2,
                    chunks=[
                        ContentChunk(
                            chunk_id="chunk-1",
                            text="后端程序运行在服务器上",
                            score=0.9,
                        ),
                        ContentChunk(
                            chunk_id="chunk-2",
                            text="用户内容存在服务器上",
                            score=0.8,
                        ),
                    ],
                )
            ],
        )
    )

    assert len(output["citations"]) == 1
    citation = output["citations"][0]
    assert citation["resource_id"] == "attachment-1"
    assert citation["evidence_ids"] == ["chunk-1", "chunk-2"]
    assert citation["quotes"] == [
        "后端程序运行在服务器上",
        "用户内容存在服务器上",
    ]


def test_knowledge_tools_are_registered_only_when_enabled() -> None:
    disabled = build_registry(
        make_settings(rag_agent_tools_enabled=False),
        InMemoryTodoStore(),
        _RAG(),
    )
    assert disabled.find("knowledge.search_sources") is None

    enabled = build_registry(
        make_settings(rag_agent_tools_enabled=True),
        InMemoryTodoStore(),
        _RAG(),
    )
    assert enabled.find("knowledge.search_sources") is not None
    assert enabled.find("knowledge.search_content") is not None
    assert enabled.find("knowledge.search_tree") is not None
    assert enabled.find("knowledge.answer") is not None


def test_routing_provider_enables_knowledge_tools_and_router() -> None:
    settings = make_settings(planner_provider="routing")
    registry = build_registry(
        settings,
        InMemoryTodoStore(),
        _RAG(),
    )

    assert registry.find("knowledge.search_sources") is not None
    assert isinstance(build_planner(settings), KnowledgeRoutingPlanner)


def test_metadata_question_routes_to_search_sources() -> None:
    route = classify_knowledge_question("张三上个月在财务群发过哪些文件？")

    assert route is not None
    assert route.mode == "sources"
    assert route.source_arguments["sender_names"] == ["张三"]
    # The stored conversation name is the bare one ("财务"); the typed suffix
    # ("群") is kept as a second acceptable filter value.
    conversation_names = route.source_arguments["conversation_names"]
    assert conversation_names[0] == "财务"
    assert "财务群" in conversation_names
    assert route.source_arguments["resource_types"] == ["attachment"]


def test_content_question_routes_to_search_content_and_answer() -> None:
    route = classify_knowledge_question("采购合同的违约责任是什么？")
    assert route is not None
    assert route.mode == "content"

    task = type(
        "Task",
        (),
        {"task_id": "task-1"},
    )()
    plan = build_knowledge_plan(
        route,
        task,
        [
            KnowledgeSearchContentCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [
        "knowledge.search_content",
        "knowledge.answer",
    ]


def test_tree_first_content_question_routes_to_search_tree_and_answer() -> None:
    route = classify_knowledge_question("采购合同的违约责任是什么？")
    assert route is not None
    assert route.mode == "content"

    task = type("Task", (), {"task_id": "task-1"})()
    plan = build_knowledge_plan(
        route,
        task,
        [
            KnowledgeSearchTreeCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
        retrieval_mode="tree_first",
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [
        "knowledge.search_tree",
        "knowledge.answer",
    ]
    assert plan.steps[0].arguments["fallback_to_content"] is True


def test_tree_first_routes_aims_server_configuration_to_tree() -> None:
    route = classify_knowledge_question("aims的服务器配置信息是什么")
    assert route is not None
    assert route.mode == "content"

    task = type("Task", (), {"task_id": "task-1"})()
    plan = build_knowledge_plan(
        route,
        task,
        [
            KnowledgeSearchTreeCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
        retrieval_mode="tree_first",
    )

    assert plan is not None
    assert plan.steps[0].capability == "knowledge.search_tree"
    assert plan.steps[1].arguments["results_ref"] == {
        "step": 1,
        "output": "results",
    }
    assert "metadata_coverage_ref" not in plan.steps[1].arguments
    assert plan.steps[1].arguments["metadata_coverage"] == (
        f"$steps.{plan.steps[0].step_id}.output.metadata_coverage"
    )


def test_knowledge_router_uses_the_original_question_not_attachment_body() -> None:
    original = "采购合同的违约责任是什么？"
    task = TaskEnvelope(
        task_id="task-attachment-query",
        source_type="chat",
        owner_user_id="user-1",
        input={
            "text": original + "\n\n--- 附件内容 ---\n" + "合同正文。" * 300,
            "_original_text": original,
            "attachment_ids": ["attachment-1"],
            "_attachment_referenced": False,
            "_attachment_excerpt": "合同正文。" * 300,
        },
        created_at=datetime.now(timezone.utc),
    )
    planner = KnowledgeRoutingPlanner(DeterministicPlanner())

    plan = planner.create_plan(
        task,
        [
            KnowledgeSearchContentCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
        [],
        PlanningConstraints(),
    )

    content_step = next(
        step
        for step in plan.steps
        if step.capability == "knowledge.search_content"
    )
    assert content_step.arguments["query"] == original


def test_knowledge_content_query_is_capped_at_schema_limit() -> None:
    route = classify_knowledge_question("采购合同的违约责任是什么？" + "补充" * 400)

    assert route is not None
    assert len(route.content_arguments["query"]) <= 500


@pytest.mark.parametrize(
    "text",
    [
        "帮我找一下青云官网当前在哪个阶段了，现在什么情况了",
        "青云官网部署到哪了",
        "谁部署的青云官网，在哪台服务器",
        "官网现在什么情况",
        "我记得我的领导之前给我说过数据库的配置，但是我忘了在哪个群里，帮我找一下",
    ],
)
def test_internal_project_status_routes_to_knowledge_content(text: str) -> None:
    route = classify_knowledge_question(text)

    assert route is not None
    assert route.mode == "content"


@pytest.mark.parametrize(
    "text",
    [
        "帮我查一下青云官网官网上的最新公开公告",
        "打开 https://www.qingcloud.com",
        "帮我上网搜一下青云官网公开信息",
    ],
)
def test_explicit_public_web_intent_does_not_route_to_knowledge(text: str) -> None:
    assert classify_knowledge_question(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "青云官网",
        "帮我查一下青云官网",
        "查一下青云官网",
    ],
)
def test_internal_entity_without_public_wording_routes_to_knowledge(
    text: str,
) -> None:
    route = classify_knowledge_question(text)

    # An internal entity on its own is a knowledge lookup; only explicit public
    # wording (checked above) may fall through to the LLM planner.
    assert route is not None
    assert route.mode == "content"


@pytest.mark.parametrize(
    ("text", "bare_name"),
    [
        ("aims群里最近在聊什么", "aims"),
        ("昨天晚上10点aims群里在聊什么", "aims"),
        ("财务群里讨论了什么", "财务"),
    ],
)
def test_chat_content_questions_route_to_filtered_sources(
    text: str, bare_name: str
) -> None:
    route = classify_knowledge_question(text)

    assert route is not None
    # A group-chat question is answered from the filtered sources, not from a
    # BM25 pass over the whole sentence.
    assert route.mode == "content_with_sources"
    names = route.source_arguments.get("conversation_names") or []
    # Collected conversations are stored under the bare name ("aims"), so the
    # typed suffix ("aims群") must not become the only filter value.
    assert names and names[0] == bare_name


def test_conversation_platform_question_routes_to_sources() -> None:
    route = classify_knowledge_question("aims群在哪个平台")

    assert route is not None
    # "which platform" is a metadata question: it is answered by the source
    # lookup, not by a content pass over the collected messages.
    assert route.mode == "sources"
    assert route.source_arguments["conversation_names"] == ["aims", "aims群"]


def test_metadata_question_builds_a_source_plan_with_an_answer() -> None:
    route = classify_knowledge_question("aims群在哪个平台")
    assert route is not None

    task = type("Task", (), {"task_id": "task-1"})()
    plan = build_knowledge_plan(
        route,
        task,
        [
            KnowledgeSearchSourcesCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
    )

    assert plan is not None
    # A source list alone does not answer "which platform": the answer step
    # reads the source records, which carry the conversation's platform.
    assert [step.capability for step in plan.steps] == [
        "knowledge.search_sources",
        "knowledge.answer",
    ]
    assert plan.steps[1].arguments["sources_ref"] == {
        "step": 1,
        "output": "sources",
    }


def test_content_with_sources_question_builds_three_step_plan() -> None:
    route = classify_knowledge_question("张三发的采购合同写了什么？")
    assert route is not None
    assert route.mode == "content_with_sources"

    task = type(
        "Task",
        (),
        {"task_id": "task-1"},
    )()
    plan = build_knowledge_plan(
        route,
        task,
        [
            KnowledgeSearchSourcesCapability.descriptor,
            KnowledgeSearchContentCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [
        "knowledge.search_sources",
        "knowledge.search_content",
        "knowledge.answer",
    ]
    assert plan.steps[1].arguments["resource_ids_ref"] == {
        "step": 1,
        "output": "resource_ids",
    }
    assert plan.steps[1].arguments["restrict_to_resource_ids"] is True


def test_knowledge_answer_empty_results_does_not_call_provider() -> None:
    provider = _AnswerProvider()
    capability = KnowledgeAnswerCapability(provider)

    output = capability.execute(
        KnowledgeAnswerInput(
            query="不存在的内容",
            results=[],
            metadata_coverage="partial",
        )
    )

    assert output["answer"] == "没有找到满足条件的内容。"
    assert output["citations"] == []
    assert output["metadata_coverage"] == "partial"
    assert output["model_calls"] == 0
    assert provider.calls == []


def test_knowledge_answer_keeps_only_known_enriched_citations() -> None:
    provider = _AnswerProvider(
        citations=[
            {"evidence_id": "chunk-1", "quote": "预算内容"},
            {"evidence_id": "invented", "quote": "不该出现"},
        ]
    )
    content = KnowledgeSearchContentCapability(_RAG())
    with bind_execution_context(context()):
        search_output = content.execute(SearchContentInput(query="预算"))
    result = search_output["results"][0]
    capability = KnowledgeAnswerCapability(provider)

    output = capability.execute(
        KnowledgeAnswerInput(query="预算内容", results=[result])
    )

    assert output["citations"] == [
        {
            "evidence_id": "chunk-1",
            "evidence_ids": ["chunk-1"],
            "quote": "预算内容",
            "quotes": ["预算内容"],
            "resource_id": "resource-1",
            "resource_type": "attachment",
            "title": "预算表.xlsx",
            "sender_name": "张三",
            "conversation_id": "conversation-1",
            "conversation_name": "财务群",
            "conversation_type": "group",
            "conversation_platform": "feishu",
            "sent_at": "2026-10-01T10:30:00+08:00",
            "position": {"paragraph_index": 2},
        }
    ]


def test_knowledge_routing_planner_intercepts_only_knowledge_questions() -> None:
    planner = KnowledgeRoutingPlanner(
        DeterministicPlanner(),
    )
    task = type(
        "Task",
        (),
        {
            "task_id": "task-1",
            "input": {"text": "张三发过哪些文件？"},
        },
    )()

    plan = planner.create_plan(
        task,
        [
            KnowledgeSearchSourcesCapability.descriptor,
            KnowledgeSearchContentCapability.descriptor,
            KnowledgeAnswerCapability.descriptor,
        ],
        [],
    )

    # The router builds the knowledge plan itself (the deterministic planner is
    # never asked), and the source list is followed by an answer so the user
    # gets a sentence as well as the matching records.
    assert [step.capability for step in plan.steps] == [
        "knowledge.search_sources",
        "knowledge.answer",
    ]


def test_personal_comparison_plan_gains_knowledge_source_without_attachment() -> None:
    class BasePlanner:
        name = "stub"
        last_call_count = 0

        def create_plan(
            self,
            task: TaskEnvelope,
            capabilities,
            observations,
            constraints,
            understanding=None,
        ) -> Plan:
            return Plan(
                plan_id="plan-personal",
                task_id=task.task_id,
                objective="比较",
                steps=[
                    PlanStep(
                        step_id="plan-personal-step-1",
                        plan_id="plan-personal",
                        order=1,
                        capability="web.research",
                        arguments={"urls": ["https://example.com/standard"]},
                    ),
                    PlanStep(
                        step_id="plan-personal-step-2",
                        plan_id="plan-personal",
                        order=2,
                        capability="answer.compose",
                        arguments={
                            "question": "判断我的公司",
                            "evidence_ref": {"step": 1, "output": "evidence"},
                        },
                    ),
                ],
            )

    task = TaskEnvelope(
        task_id="task-personal",
        source_type="chat",
        owner_user_id="user-1",
        input={
            "text": "https://example.com/standard 根据这个网址判断我的公司是否符合标准"
        },
        created_at=datetime.now(timezone.utc),
    )
    planner = KnowledgeRoutingPlanner(BasePlanner())

    plan = planner.create_plan(
        task,
        [
            KnowledgeSearchContentCapability.descriptor,
            WebResearchCapability.descriptor,
            AnswerComposeCapability.descriptor,
        ],
        [],
        PlanningConstraints(),
    )

    assert [step.capability for step in plan.steps] == [
        "knowledge.search_content",
        "web.research",
        "answer.compose",
    ]
    assert plan.steps[0].arguments["include_personal"] is True
    assert plan.steps[2].arguments["knowledge_evidence_refs"] == [
        {"step": 1, "output": "evidence"}
    ]
    assert plan.steps[2].arguments["evidence_ref"] == {"step": 2, "output": "evidence"}


def test_existing_personal_knowledge_step_gets_an_attribute_query_and_answer_ref() -> None:
    class BasePlanner:
        name = "stub"
        last_call_count = 0

        def create_plan(
            self,
            task: TaskEnvelope,
            capabilities,
            observations,
            constraints,
            understanding=None,
        ) -> Plan:
            return Plan(
                plan_id="plan-personal-existing",
                task_id=task.task_id,
                objective="比较",
                steps=[
                    PlanStep(
                        step_id="plan-personal-existing-step-1",
                        plan_id="plan-personal-existing",
                        order=1,
                        capability="web.research",
                        arguments={"urls": ["https://example.com/standard"]},
                    ),
                    PlanStep(
                        step_id="plan-personal-existing-step-2",
                        plan_id="plan-personal-existing",
                        order=2,
                        capability="knowledge.search_content",
                        arguments={
                            "query": "我的公司是否是独角兽企业",
                            "include_personal": False,
                        },
                    ),
                    PlanStep(
                        step_id="plan-personal-existing-step-3",
                        plan_id="plan-personal-existing",
                        order=3,
                        capability="answer.compose",
                        arguments={
                            "question": "判断我的公司",
                            "knowledge_evidence": [],
                            "evidence_ref": {"step": 1, "output": "evidence"},
                        },
                    ),
                ],
            )

    task = TaskEnvelope(
        task_id="task-personal-existing",
        source_type="chat",
        owner_user_id="user-1",
        input={
            "text": "https://example.com/standard 根据这个网址判断我的公司是否是独角兽企业"
        },
        created_at=datetime.now(timezone.utc),
    )
    planner = KnowledgeRoutingPlanner(BasePlanner())

    plan = planner.create_plan(
        task,
        [
            KnowledgeSearchContentCapability.descriptor,
            WebResearchCapability.descriptor,
            AnswerComposeCapability.descriptor,
        ],
        [],
        PlanningConstraints(),
    )

    knowledge_step = next(
        step for step in plan.steps if step.capability == "knowledge.search_content"
    )
    answer_step = next(
        step for step in plan.steps if step.capability == "answer.compose"
    )
    assert knowledge_step.arguments["include_personal"] is True
    assert "公司简介" in knowledge_step.arguments["query"]
    assert "估值" in knowledge_step.arguments["query"]
    assert "knowledge_evidence" not in answer_step.arguments
    assert answer_step.arguments["knowledge_evidence_refs"] == [
        {"step": knowledge_step.order, "output": "evidence"}
    ]


def test_composite_source_plan_continues_after_a_successful_step() -> None:
    class FailIfCalledPlanner:
        name = "stub"
        last_call_count = 0

        def decide_after_observation(self, *args, **kwargs):
            raise AssertionError("the base planner must not replan a viable composite plan")

    plan = Plan(
        plan_id="plan-composite",
        task_id="task-1",
        objective="多来源比较",
        steps=[
            PlanStep(
                step_id="plan-composite-step-1",
                plan_id="plan-composite",
                order=1,
                capability="knowledge.search_content",
                status="succeeded",
            ),
            PlanStep(
                step_id="plan-composite-step-2",
                plan_id="plan-composite",
                order=2,
                capability="web.research",
                status="pending",
            ),
            PlanStep(
                step_id="plan-composite-step-3",
                plan_id="plan-composite",
                order=3,
                capability="answer.compose",
                status="pending",
            ),
        ],
    )
    observation = Observation(
        observation_id="obs-1",
        task_id="task-1",
        plan_id=plan.plan_id,
        step_id="plan-composite-step-1",
        capability="knowledge.search_content",
        status="succeeded",
        output={"evidence": [{"evidence_id": "doc-1"}]},
        created_at=datetime.now(timezone.utc),
    )
    planner = KnowledgeRoutingPlanner(FailIfCalledPlanner())

    decision = planner.decide_after_observation(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={"text": "比较"},
            created_at=datetime.now(timezone.utc),
        ),
        plan,
        [observation],
        PlanningConstraints(),
    )

    assert decision.action == "continue"


def test_knowledge_plan_with_answer_compose_closes_without_the_base_planner() -> None:
    class FailIfCalledPlanner:
        name = "stub"
        last_call_count = 0

        def decide_after_observation(self, *args, **kwargs):
            raise AssertionError("a knowledge plan must not pay for a model decision")

    plan = Plan(
        plan_id="plan-knowledge-compose",
        task_id="task-1",
        objective="回答知识问题",
        steps=[
            PlanStep(
                step_id="plan-knowledge-compose-step-1",
                plan_id="plan-knowledge-compose",
                order=1,
                capability="knowledge.search_content",
                status="succeeded",
            ),
            PlanStep(
                step_id="plan-knowledge-compose-step-2",
                plan_id="plan-knowledge-compose",
                order=2,
                capability="answer.compose",
                status="pending",
            ),
        ],
    )
    observation = Observation(
        observation_id="obs-1",
        task_id="task-1",
        plan_id=plan.plan_id,
        step_id="plan-knowledge-compose-step-1",
        capability="knowledge.search_content",
        status="succeeded",
        output={"evidence": [{"evidence_id": "doc-1"}]},
        created_at=datetime.now(timezone.utc),
    )
    planner = KnowledgeRoutingPlanner(FailIfCalledPlanner())

    decision = planner.decide_after_observation(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={"text": "数据库密码是多少"},
            created_at=datetime.now(timezone.utc),
        ),
        plan,
        [observation],
        PlanningConstraints(),
    )

    assert decision.action == "continue"


def test_confident_web_research_is_not_overridden_by_internal_object_words() -> None:
    class WebPlanner:
        name = "routing"
        last_call_count = 0

        def create_plan(
            self,
            task,
            capabilities,
            observations,
            constraints=None,
            understanding=None,
        ):
            return Plan(
                plan_id="plan-web",
                task_id=task.task_id,
                objective="联网搜索",
                steps=[
                    PlanStep(
                        step_id="plan-web-step-1",
                        plan_id="plan-web",
                        order=1,
                        capability="web.research",
                    )
                ],
            )

    planner = KnowledgeRoutingPlanner(WebPlanner())
    understanding = TaskUnderstanding(
        is_task=True,
        goal="搜索飞书开放平台",
        task_kind="action",
        intent_candidates=[
            UnderstandingIntent(name="web.research", confidence=1.0)
        ],
        confidence=1.0,
    )
    plan = planner.create_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={"text": "帮我搜索一下飞书开放平台是什么，并总结一下"},
            created_at=datetime.now(timezone.utc),
        ),
        [
            CapabilityDescriptor(
                name="web.research",
                description="web",
                risk_level="read_only",
                side_effect=False,
                requires_approval=False,
                idempotent=True,
                timeout_seconds=90,
            )
        ],
        [],
        PlanningConstraints(),
        understanding,
    )

    assert [step.capability for step in plan.steps] == ["web.research"]


def test_compliance_assessment_builds_company_plus_web_comparison() -> None:
    plan = build_compliance_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={
                "text": (
                    "请阅读 https://example.com/protocol.pdf 并说明我们公司是否符合"
                    "该协议，哪些符合、哪些不符合"
                )
            },
            created_at=datetime.now(timezone.utc),
        ),
        [
            _descriptor("knowledge.search_content"),
            _descriptor("web.research"),
            _descriptor("answer.compose"),
        ],
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [
        "knowledge.search_content",
        "web.research",
        "answer.compose",
    ]
    answer = plan.steps[-1]
    assert answer.arguments["knowledge_evidence_refs"] == [
        {"step": 1, "output": "evidence"}
    ]
    assert answer.arguments["evidence_refs"] == [
        {"step": 2, "output": "evidence"}
    ]


def test_compliance_plan_uses_uploaded_attachment_as_company_evidence() -> None:
    plan = build_compliance_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={
                "text": (
                    "请结合我上传的附件和 https://example.com/protocol.pdf，"
                    "说明我们公司是否符合该协议"
                ),
                "attachment_ids": ["att-1"],
                "_attachment_processed": True,
                "_attachment_referenced": True,
                "_attachment_excerpt": "公司制度：账号由张三管理，密码每 90 天更换。",
                "_attachment_file_names": ["公司制度.txt"],
            },
            created_at=datetime.now(timezone.utc),
        ),
        [
            _descriptor("web.research"),
            _descriptor("answer.compose"),
        ],
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [
        "web.research",
        "answer.compose",
    ]
    answer = plan.steps[-1]
    evidence = answer.arguments["attachment_evidence"]
    assert evidence[0]["fetch_method"] == "attachment"
    assert evidence[0]["title"] == "公司制度.txt"
    assert "张三" in evidence[0]["text"]


def test_compliance_web_query_drops_the_company_material_clause() -> None:
    plan = build_compliance_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={
                "text": (
                    "请你搜索北京市网络协议，然后结合我们公司已采集的材料，"
                    "说明我们公司是否符合，哪些符合哪些不符合"
                )
            },
            created_at=datetime.now(timezone.utc),
        ),
        [
            _descriptor("knowledge.search_content"),
            _descriptor("web.research"),
            _descriptor("answer.compose"),
        ],
    )

    assert plan is not None
    web = next(step for step in plan.steps if step.capability == "web.research")
    assert web.arguments["queries"] == ["请你搜索北京市网络协议，"]


def test_attachment_plus_url_comparison_uses_both_sources() -> None:
    class NeverPlanner:
        name = "never"
        last_call_count = 0

        def create_plan(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the comparison plan is deterministic")

    planner = KnowledgeRoutingPlanner(NeverPlanner())
    task = TaskEnvelope(
        task_id="task-1",
        source_type="chat",
        owner_user_id="user-1",
        input={
            "text": (
                "根据我上传的附件和 https://example.com/protocol.pdf，"
                "说明我们公司哪些符合、哪些不符合"
            ),
            "attachment_ids": ["att-1"],
            "_attachment_processed": True,
            "_attachment_referenced": True,
            "_attachment_excerpt": "公司制度：数据每周备份一次。",
            "_attachment_file_names": ["公司制度.docx"],
        },
        created_at=datetime.now(timezone.utc),
    )
    understanding = TaskUnderstanding(
        is_task=True,
        goal="附件与协议对比",
        task_kind="mixed",
        intent_candidates=[
            UnderstandingIntent(name="knowledge.answer", confidence=0.95)
        ],
        confidence=0.95,
    )

    plan = planner.create_plan(
        task,
        [
            _descriptor("web.research"),
            _descriptor("answer.compose"),
        ],
        [],
        PlanningConstraints(),
        understanding,
    )

    assert [step.capability for step in plan.steps] == [
        "web.research",
        "answer.compose",
    ]
    answer = plan.steps[-1]
    assert answer.arguments["attachment_evidence"][0]["fetch_method"] == "attachment"
    assert answer.arguments["evidence_refs"] == [
        {"step": 1, "output": "evidence"}
    ]


def test_action_phrase_does_not_take_the_knowledge_route() -> None:
    class ActionPlanner:
        name = "deterministic"
        last_call_count = 0

        def create_plan(
            self,
            task,
            capabilities,
            observations,
            constraints=None,
            understanding=None,
        ):
            return Plan(
                plan_id="plan-todo",
                task_id=task.task_id,
                objective="创建待办",
                steps=[
                    PlanStep(
                        step_id="plan-todo-step-1",
                        plan_id="plan-todo",
                        order=1,
                        capability="todo.create",
                    )
                ],
            )

    planner = KnowledgeRoutingPlanner(ActionPlanner())
    understanding = TaskUnderstanding(
        is_task=True,
        goal="完成登录模块代码",
        task_kind="action",
        intent_candidates=[],
        confidence=0.99,
    )
    plan = planner.create_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={"text": "完成登录模块代码"},
            created_at=datetime.now(timezone.utc),
        ),
        [_descriptor("todo.create"), _descriptor("knowledge.search_content")],
        [],
        PlanningConstraints(),
        understanding,
    )

    assert [step.capability for step in plan.steps] == ["todo.create"]


def test_form_complete_builds_the_fixed_preview_apply_plan() -> None:
    plan = build_form_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={
                "text": "打开表单 http://127.0.0.1:8765/form2 ，项目是 aims，填写并提交。"
            },
            created_at=datetime.now(timezone.utc),
        ),
        [_descriptor("form.preview"), _descriptor("form.apply")],
    )

    assert plan is not None
    assert [step.capability for step in plan.steps] == [
        "form.preview",
        "form.apply",
    ]
    assert plan.steps[0].arguments["url"] == "http://127.0.0.1:8765/form2"
    assert plan.steps[1].arguments["draft_ref"] == {
        "step": 1,
        "output": "form",
    }
    assert plan.steps[1].arguments["action"] == "fill_and_submit"


def test_form_complete_intent_skips_the_llm_planner() -> None:
    class NeverPlanner:
        name = "never"
        last_call_count = 0

        def create_plan(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("form.complete uses the fixed pipeline")

    planner = KnowledgeRoutingPlanner(NeverPlanner())
    understanding = TaskUnderstanding(
        is_task=True,
        goal="填写并提交表单",
        task_kind="action",
        intent_candidates=[
            UnderstandingIntent(name="form.complete", confidence=0.95)
        ],
        confidence=0.95,
    )
    plan = planner.create_plan(
        TaskEnvelope(
            task_id="task-1",
            source_type="chat",
            owner_user_id="user-1",
            input={"text": "打开 http://127.0.0.1:8765/form2 填写并提交"},
            created_at=datetime.now(timezone.utc),
        ),
        [_descriptor("form.preview"), _descriptor("form.apply")],
        [],
        PlanningConstraints(),
        understanding,
    )

    assert [step.capability for step in plan.steps] == [
        "form.preview",
        "form.apply",
    ]


def test_routing_planner_executes_content_pipeline_end_to_end() -> None:
    rag = _RAG()
    answer = _AnswerProvider()
    registry = CapabilityRegistry(
        [
            KnowledgeSearchSourcesCapability(rag),
            KnowledgeSearchContentCapability(rag),
            KnowledgeAnswerCapability(answer),
        ]
    )
    store = InMemoryAgentStore()
    container = build_container(
        settings=make_settings(planner_provider="routing"),
        store=store,
        todo_store=InMemoryTodoStore(),
        publisher=FakeTaskPublisher(),
        registry=registry,
        planner=KnowledgeRoutingPlanner(DeterministicPlanner()),
        policy=DescriptorPolicy(registry),
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "预算表里写了什么？"},
        source_ref={"organization_id": "org-1"},
    )

    status = container.execution_service.run_task(task.task_id)

    assert status == "succeeded"
    observations = store.list_observations(task.task_id)
    assert [item.capability for item in observations] == [
        "knowledge.search_content",
        "knowledge.answer",
    ]
    assert answer.calls
    completed = store.get_task(task.task_id)
    assert completed.result["answer"] == "检索到的回答"
    assert completed.result["presentation_mode"] == "answer_with_sources"
    previews = [
        event.payload.get("result_preview")
        for event in store.list_events(task.task_id)
        if event.event_type == "step.succeeded"
    ]
    assert {"block_type": "content_results", "summary": "在1个资源中找到与“预算表里写了什么？”相关的内容", "item_count": 1, "chunk_count": 1, "metadata_coverage": "complete"} in previews
    assert any(
        item
        and item.get("block_type") == "answer"
        and item.get("citation_count") == 1
        for item in previews
    )
