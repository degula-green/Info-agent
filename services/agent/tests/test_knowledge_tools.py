from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.capabilities.knowledge import (
    KnowledgeSearchContentCapability,
    KnowledgeSearchSourcesCapability,
    KnowledgeToolUnavailable,
    SearchContentInput,
    SearchSourcesInput,
)
from app.container import build_registry
from app.testing.in_memory_todo_store import InMemoryTodoStore
from tests.support import make_settings
from app.kernel.execution_context import (
    ExecutionContext,
    bind_execution_context,
    current_execution_context,
)
from app.kernel.bindings import bind_plan_references
from app.kernel.models import Plan, PlanStep


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
        return {
            "items": [
                {
                    "resource_id": "resource-1",
                    "resource_type": "attachment",
                    "title": "预算表.xlsx",
                    "sender": {"name": "张三"},
                    "conversation": {"name": "财务群"},
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
            "has_more": False,
            "diagnostics": {"metadata_coverage": "complete"},
        }


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


def test_content_capability_declares_resource_ids_binding() -> None:
    descriptor = KnowledgeSearchContentCapability.descriptor
    assert descriptor.input_bindings[0].planner_argument == "resource_ids_ref"
    assert descriptor.input_bindings[0].runtime_argument == "resource_ids"
    assert descriptor.input_bindings[0].source_output == "resource_ids"


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
