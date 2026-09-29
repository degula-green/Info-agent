"""LLM planner: reference syntax and unusable-argument repair.

These pin the behaviour that matters when a model, not a pure function, writes
the plan: the arguments it invents must match the capability's own schema, and
following a repaired plan must not break the runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.capabilities.todo import TodoCreateCapability
from app.capabilities.web import WebExtractCapability, WebFetchCapability
from app.kernel.models import (
    CapabilityDescriptor,
    Plan,
    PlanningConstraints,
    PlanStep,
    TaskEnvelope,
)
from app.planning.llm import OpenAICompatiblePlanner, PlannerValidationError
from app.testing.fake_providers import FakePageFetcher
from app.testing.in_memory_todo_store import InMemoryTodoStore


class StubPlannerClient:
    model = "stub-planner"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []
        self.last_call_count = 1

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.outputs.pop(0)


def envelope(text: str = "明天晚上八点跟张三开评审会") -> TaskEnvelope:
    return TaskEnvelope(
        task_id="task-llm-planner",
        source_type="chat",
        owner_user_id="user-1",
        input={"text": text},
        created_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
    )


def todo_descriptor() -> CapabilityDescriptor:
    return TodoCreateCapability(InMemoryTodoStore()).descriptor


def test_an_invalid_argument_is_repaired_before_the_plan_is_returned() -> None:
    """The first draft uses a field todo.create does not accept."""

    bad = (
        '{"objective": "创建待办", "steps": ['
        '{"capability": "todo.create", "arguments": {"title": "开会", "time": "明天", '
        '"owner_user_id": "user-1", "idempotency_key": "k1"}}]}'
    )
    good = (
        '{"objective": "创建待办", "steps": ['
        '{"capability": "todo.create", "arguments": {"title": "开会", "due_expression": "明天", '
        '"owner_user_id": "user-1", "idempotency_key": "k1"}}]}'
    )
    client = StubPlannerClient([bad, good])
    planner = OpenAICompatiblePlanner(client)
    planner.set_validators({"todo.create": TodoCreateCapability(InMemoryTodoStore()).validate})

    plan = planner.create_plan(envelope(), [todo_descriptor()], [], PlanningConstraints())

    assert len(client.calls) == 2
    assert "due_expression" in plan.steps[0].arguments
    assert "time" not in plan.steps[0].arguments


def test_a_usable_first_draft_is_not_repaired() -> None:
    good = (
        '{"objective": "创建待办", "steps": ['
        '{"capability": "todo.create", "arguments": {"title": "开会", "owner_user_id": "user-1", '
        '"idempotency_key": "k1"}}]}'
    )
    client = StubPlannerClient([good])
    planner = OpenAICompatiblePlanner(client)
    planner.set_validators({"todo.create": TodoCreateCapability(InMemoryTodoStore()).validate})

    plan = planner.create_plan(envelope(), [todo_descriptor()], [], PlanningConstraints())

    assert len(client.calls) == 1
    assert plan.steps[0].arguments["title"] == "开会"


def test_a_reference_argument_is_not_treated_as_invalid(monkeypatch) -> None:
    """A placeholder cannot be schema-checked before Runtime resolves it.

    The step ids are fixed here because the prompt has to name them: the model
    can only write the reference the system message told it to write.
    """

    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")

    draft = (
        '{"objective": "读链接再填表", "steps": ['
        '{"capability": "web.fetch", "arguments": {"url": "https://example.com"}}, '
        '{"capability": "todo.create", "arguments": {"title": "$steps.plan-fixed-step-1.output.text", '
        '"owner_user_id": "user-1", "idempotency_key": "k1"}}]}'
    )
    client = StubPlannerClient([draft])
    planner = OpenAICompatiblePlanner(client)
    planner.set_validators({"todo.create": TodoCreateCapability(InMemoryTodoStore()).validate})
    descriptors = [
        CapabilityDescriptor(
            name="web.fetch",
            description="只读抓取",
            risk_level="read_only",
            side_effect=False,
            requires_approval=False,
            idempotent=True,
            timeout_seconds=30,
        ),
        todo_descriptor(),
    ]

    plan = planner.create_plan(envelope("读链接再填表"), descriptors, [], PlanningConstraints())

    assert len(client.calls) == 1
    assert len(plan.steps) == 2
    assert plan.steps[1].arguments["title"] == "$steps.plan-fixed-step-1.output.text"

def _plan(plan_id: str = "plan-1") -> Plan:
    return Plan(
        plan_id=plan_id,
        task_id="task-llm-planner",
        objective="创建待办",
        steps=[
            PlanStep(
                step_id=f"{plan_id}-step-1",
                plan_id=plan_id,
                order=1,
                capability="todo.create",
                arguments={"title": "开会", "owner_user_id": "user-1"},
            )
        ],
    )


def test_a_malformed_decision_degrades_to_fail_instead_of_raising() -> None:
    """A bad decision is a bad answer, not a crash.

    The model wrote a decision field at the top level, which DecisionDraft
    forbids. Previously this escaped the planner as an exception and the Task
    died with a stack trace; it must instead come back as a readable ``fail``.
    """

    bad = '{"action": "replan", "prompt": "重试一下"}'
    client = StubPlannerClient([bad, bad])
    planner = OpenAICompatiblePlanner(client)

    decision = planner.decide_after_observation(
        envelope(), _plan(), [], PlanningConstraints()
    )

    assert decision.action == "fail"
    assert decision.reason and "could not be parsed" in decision.reason
    assert decision.plan is None


def test_an_unknown_action_degrades_to_fail_instead_of_raising() -> None:
    """``request_input`` is an action; a model that puts it in ``steps`` is lost."""

    bad = '{"action": "nonexistent", "reason": "?"}'
    client = StubPlannerClient([bad])
    planner = OpenAICompatiblePlanner(client)

    decision = planner.decide_after_observation(
        envelope(), _plan(), [], PlanningConstraints()
    )

    assert decision.action == "fail"
    assert "nonexistent" in (decision.reason or "")


def test_a_valid_decision_still_passes_through() -> None:
    """The degradation must not swallow a decision that is actually fine."""

    good = (
        '{"action": "request_input", "required_input": ["截止时间"], '
        '"reason": "缺少时间"}'
    )
    client = StubPlannerClient([good])
    planner = OpenAICompatiblePlanner(client)

    decision = planner.decide_after_observation(
        envelope(), _plan(), [], PlanningConstraints()
    )

    assert decision.action == "request_input"
    assert decision.required_input == ["截止时间"]
    assert decision.reason == "缺少时间"

def test_a_required_input_written_as_a_string_is_still_request_input() -> None:
    """A missing bracket must not turn a good request_input into a fail.

    Observed from qwen-plus: it answered with the right action but wrote
    ``"required_input": "请提供文件"``. Rejecting the whole answer failed the
    Task over punctuation, losing an intent that was perfectly clear.
    """

    good = (
        '{"action": "request_input", "required_input": "请提供文件", '
        '"reason": "缺少来源"}'
    )
    client = StubPlannerClient([good])
    planner = OpenAICompatiblePlanner(client)

    decision = planner.decide_after_observation(
        envelope(), _plan(), [], PlanningConstraints()
    )

    assert decision.action == "request_input"
    assert decision.required_input == ["请提供文件"]
    assert len(client.outputs) == 0


def reading_descriptors() -> list[CapabilityDescriptor]:
    return [
        WebFetchCapability(FakePageFetcher()).descriptor,
        WebExtractCapability().descriptor,
    ]


def test_a_strict_reference_draft_is_bound_before_the_plan_is_returned(
    monkeypatch,
) -> None:
    """The model names step 1 instead of copying its output into the plan."""

    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    draft = (
        '{"objective": "读网页", "steps": ['
        '{"capability": "web.fetch", "arguments": {"url": "https://example.com"}}, '
        '{"capability": "web.extract", '
        '"arguments": {"document_ref": {"step": 1, "output": "content"}}}]}'
    )
    client = StubPlannerClient([draft])
    planner = OpenAICompatiblePlanner(client)

    plan = planner.create_plan(
        envelope("读网页"), reading_descriptors(), [], PlanningConstraints()
    )

    assert len(client.calls) == 1
    assert plan.steps[1].arguments == {
        "document": "$steps.plan-fixed-step-1.output.content"
    }


def test_an_unbindable_reference_is_repaired_once(monkeypatch) -> None:
    """A reference to a future step is a bad answer, not a dead Task."""

    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    bad = (
        '{"objective": "读网页", "steps": ['
        '{"capability": "web.extract", '
        '"arguments": {"document_ref": {"step": 2, "output": "content"}}}, '
        '{"capability": "web.fetch", "arguments": {"url": "https://example.com"}}]}'
    )
    good = (
        '{"objective": "读网页", "steps": ['
        '{"capability": "web.fetch", "arguments": {"url": "https://example.com"}}, '
        '{"capability": "web.extract", '
        '"arguments": {"document_ref": {"step": 1, "output": "content"}}}]}'
    )
    client = StubPlannerClient([bad, good])
    planner = OpenAICompatiblePlanner(client)

    plan = planner.create_plan(
        envelope("读网页"), reading_descriptors(), [], PlanningConstraints()
    )

    assert len(client.calls) == 2
    repair_prompt = client.calls[1][-1]["content"]
    assert "earlier step" in repair_prompt  # the binder's own reason is passed on
    assert "更早步骤" in repair_prompt  # along with the instruction that fixes it
    assert plan.steps[1].arguments["document"] == (
        "$steps.plan-fixed-step-1.output.content"
    )


def test_a_reference_that_stays_unbindable_fails_the_plan(monkeypatch) -> None:
    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    bad = (
        '{"objective": "读网页", "steps": ['
        '{"capability": "web.extract", '
        '"arguments": {"document_ref": {"step": 2, "output": "content"}}}, '
        '{"capability": "web.fetch", "arguments": {"url": "https://example.com"}}]}'
    )
    client = StubPlannerClient([bad, bad])
    planner = OpenAICompatiblePlanner(client)

    with pytest.raises(PlannerValidationError) as excinfo:
        planner.create_plan(
            envelope("读网页"), reading_descriptors(), [], PlanningConstraints()
        )

    assert "earlier" in str(excinfo.value)
    assert len(client.calls) == 2


class StructuredStubClient(StubPlannerClient):
    """Records the schema the planner asked for instead of sending it."""

    def __init__(self, outputs: list[str]) -> None:
        super().__init__(outputs)
        self.schemas: list[tuple[str, dict]] = []

    def complete_structured(
        self, messages: list[dict[str, str]], *, schema: dict, name: str = "x"
    ) -> str:
        self.schemas.append((name, schema))
        return super().complete(messages)


def test_the_planner_asks_for_the_capability_schema(monkeypatch) -> None:
    monkeypatch.setattr("app.planning.llm.uuid4", lambda: "plan-fixed")
    draft = (
        '{"objective": "读网页", "steps": ['
        '{"capability": "web.fetch", "arguments": {"url": "https://example.com"}}, '
        '{"capability": "web.extract", '
        '"arguments": {"document_ref": {"step": 1, "output": "content"}}}]}'
    )
    client = StructuredStubClient([draft])
    planner = OpenAICompatiblePlanner(client)

    planner.create_plan(
        envelope("读网页"), reading_descriptors(), [], PlanningConstraints()
    )

    name, schema = client.schemas[0]
    assert name == "agent_plan"
    rendered = json.dumps(schema, ensure_ascii=False)
    assert "document_ref" in rendered
    assert "web.fetch" in rendered and "web.extract" in rendered
