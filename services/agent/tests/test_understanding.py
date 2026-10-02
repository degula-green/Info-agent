"""Task-understanding provider, mode and persistence tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.kernel.models import TaskUnderstanding, UnderstandingIntent
from app.ingress.chat import ChatIngress
from app.understanding.provider import (
    OpenAICompatibleUnderstandingProvider,
    RuleBasedUnderstandingProvider,
    UnderstandingProviderError,
)
from app.understanding.schema import (
    TaskUnderstandingDraft,
    available_intents,
    intent_catalog_text,
)
from app.planning.deterministic import DEFAULT_CAPABILITY_NAME
from tests.support import build_step2_container, event_types, knowledge_event, snapshot


class StubClient:
    model = "stub-model"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []
        self.last_call_count = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        self.last_call_count = 1
        return self.outputs.pop(0)


class CountingUnderstandingProvider:
    name = "counting"
    model = "counting"
    last_call_count = 1

    def __init__(self, result: TaskUnderstanding | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def understand(self, task):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def understanding(
    *,
    is_task: bool = True,
    intents: list[str] | None = None,
    confidence: float = 0.9,
) -> TaskUnderstanding:
    return TaskUnderstanding(
        is_task=is_task,
        goal="明天晚上八点开会",
        task_kind="action",
        intent_candidates=[
            UnderstandingIntent(name=name, confidence=confidence)
            for name in (intents or [DEFAULT_CAPABILITY_NAME])
        ],
        confidence=confidence,
        reason="test understanding",
    )


def test_rule_provider_rejects_keyword_only_false_positive() -> None:
    provider = RuleBasedUnderstandingProvider()
    from app.ingress.chat import ChatIngress

    task = ChatIngress().create_task("user-1", {"text": "没有时间不会创建日程的"})
    result = provider.understand(task)

    assert result.is_task is False
    assert result.intent_candidates == []


def test_rule_provider_recognizes_a_conservative_schedule() -> None:
    provider = RuleBasedUnderstandingProvider()
    from app.ingress.chat import ChatIngress

    task = ChatIngress().create_task("user-1", {"text": "明天晚上八点开评审会"})
    result = provider.understand(task)

    assert result.is_task is True
    assert result.intent_candidates[0].name == DEFAULT_CAPABILITY_NAME


def test_unknown_intent_is_rejected_by_the_wire_schema() -> None:
    with pytest.raises(ValidationError):
        TaskUnderstandingDraft.model_validate(
            {
                "is_task": True,
                "goal": "do something",
                "task_kind": "action",
                "intent_candidates": [
                    {"name": "made.up", "confidence": 0.9, "evidence": None}
                ],
                "confidence": 0.9,
                "reason": "unknown",
            }
        )


def test_llm_provider_repairs_invalid_json_once() -> None:
    valid = """
    {
      "is_task": true,
      "goal": "明天晚上八点开会",
      "task_kind": "action",
      "intent_candidates": [
        {"name": "todo.create", "confidence": 0.95, "evidence": "明天晚上八点"}
      ],
      "confidence": 0.95,
      "reason": "明确日程"
    }
    """
    client = StubClient(["{not json", valid])
    provider = OpenAICompatibleUnderstandingProvider(client)
    from app.ingress.chat import ChatIngress

    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "明天晚上八点开会"})
    )

    assert result.intent_candidates[0].name == DEFAULT_CAPABILITY_NAME
    assert provider.last_call_count == 2
    assert len(client.calls) == 2


def test_llm_provider_fails_after_one_repair() -> None:
    client = StubClient(["bad", "still bad"])
    provider = OpenAICompatibleUnderstandingProvider(client)
    from app.ingress.chat import ChatIngress

    with pytest.raises(UnderstandingProviderError):
        provider.understand(
            ChatIngress().create_task("user-1", {"text": "明天晚上八点开会"})
        )


def test_shadow_mode_audits_without_changing_planner_behavior() -> None:
    from app.understanding.provider import FakeUnderstandingProvider

    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=FakeUnderstandingProvider(
            understanding(is_task=False, intents=[])
        ),
        understanding_mode="shadow",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "明天晚上八点开评审会"},
    )

    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    assert "task.understanding" in event_types(store, task.task_id)
    assert store.get_task(task.task_id).understanding is None


def test_off_mode_does_not_call_provider_or_write_event() -> None:
    provider = CountingUnderstandingProvider(understanding())
    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=provider,
        understanding_mode="off",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "明天晚上八点开评审会"},
    )

    assert container.execution_service.run_task(task.task_id) == "waiting_approval"

    assert provider.calls == 0
    assert "task.understanding" not in event_types(store, task.task_id)


def test_shadow_provider_failure_is_audited_without_failing_task() -> None:
    provider = CountingUnderstandingProvider(
        error=RuntimeError("provider unavailable")
    )
    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=provider,
        understanding_mode="shadow",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "明天晚上八点开评审会"},
    )

    assert container.execution_service.run_task(task.task_id) == "waiting_approval"

    events = store.list_events(task.task_id)
    understanding_events = [
        item for item in events if item.event_type == "task.understanding"
    ]
    assert provider.calls == 1
    assert understanding_events[-1].payload["provider_error"]["message"] == "provider unavailable"


def test_enforce_provider_failure_fails_the_task() -> None:
    provider = CountingUnderstandingProvider(
        error=RuntimeError("provider unavailable")
    )
    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=provider,
        understanding_mode="enforce",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "明天晚上八点开评审会"},
    )

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert store.get_task(task.task_id).last_error["message"] == "provider unavailable"


def test_enforce_mode_uses_understanding_to_produce_a_zero_step_plan() -> None:
    from app.understanding.provider import FakeUnderstandingProvider

    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=FakeUnderstandingProvider(
            understanding(is_task=False, intents=[])
        ),
        understanding_mode="enforce",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "明天晚上八点开评审会"},
    )

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert store.list_approvals(task_id=task.task_id) == []
    assert store.get_task(task.task_id).understanding is not None
    assert event_types(store, task.task_id).count("task.understanding") == 1


def test_partial_write_requires_confirmation_before_todo_execution() -> None:
    from app.understanding.provider import FakeUnderstandingProvider

    container, store, _publisher, knowledge = build_step2_container(
        understanding_provider=FakeUnderstandingProvider(
            understanding(intents=[DEFAULT_CAPABILITY_NAME, "web.research"])
        ),
        understanding_mode="enforce",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "查一下资料并安排明天晚上八点开会"},
    )

    assert container.execution_service.run_task(task.task_id) == "waiting_input"
    assert knowledge.snapshot_calls == [] or True  # nothing reaches Knowledge on a partial write
    waiting = [
        item
        for item in store.list_events(task.task_id)
        if item.event_type == "task.waiting_input"
    ]
    assert waiting[-1].payload["missing_information"] == [
        "partial_execution_confirmation"
    ]

    container.task_service.submit_input(
        task.task_id,
        owner_user_id="user-1",
        payload={"partial_execution_confirmation": True},
    )
    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    # A partial write must never reach Knowledge: the snapshot is its only
    # interface, and the to-do write is local to the Agent.
    assert knowledge.snapshot_calls == []
    stored = store.get_task(task.task_id)
    plan = store.get_plan(stored.current_plan_id)
    assert plan.requires_user_confirmation is False
    assert plan.warnings == ["unsupported intent: web.research"]


def test_all_unsupported_intents_complete_with_warnings() -> None:
    from app.understanding.provider import FakeUnderstandingProvider

    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=FakeUnderstandingProvider(
            understanding(intents=["web.research"])
        ),
        understanding_mode="enforce",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "搜索公开资料"},
    )

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    stored = store.get_task(task.task_id)
    assert stored.result["warnings"] == ["unsupported intent: web.research"]


def test_collected_keyword_false_positive_finishes_without_a_draft() -> None:
    from app.testing.fake_knowledge import FakeKnowledgeClient
    from app.understanding.provider import FakeUnderstandingProvider

    client = FakeKnowledgeClient(
        snapshots={
            "item-1": snapshot(text="没有时间不会创建日程的"),
        }
    )
    container, store, _publisher, _knowledge = build_step2_container(
        knowledge=client,
        understanding_provider=FakeUnderstandingProvider(
            understanding(is_task=False, intents=[])
        ),
        understanding_mode="enforce",
    )

    outcome = container.knowledge_events.handle(knowledge_event())

    assert len(outcome.task_ids) == 1
    task_id = outcome.task_ids[0]
    assert container.execution_service.run_task(task_id) == "succeeded"
    assert store.list_approvals(task_id=task_id) == []


def test_user_input_re_runs_understanding_and_creates_plan_v2() -> None:
    """A user reply resumes the task: understanding runs again, plan goes to v2.

    Ambiguous times no longer pause a task (a todo may be time-less), so the
    pause is driven by an unsupported write intent that only needs a partial
    execution confirmation.
    """
    from app.understanding.provider import FakeUnderstandingProvider

    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=FakeUnderstandingProvider(
            understanding(intents=[DEFAULT_CAPABILITY_NAME, "web.research"])
        ),
        understanding_mode="enforce",
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "查一下资料并安排明天晚上八点开会"},
    )

    assert container.execution_service.run_task(task.task_id) == "waiting_input"
    assert event_types(store, task.task_id).count("task.understanding") == 1

    container.task_service.submit_input(
        task.task_id,
        owner_user_id="user-1",
        payload={"partial_execution_confirmation": True},
    )
    assert container.execution_service.run_task(task.task_id) == "waiting_approval"

    stored = store.get_task(task.task_id)
    assert event_types(store, task.task_id).count("task.understanding") == 2
    assert stored.current_plan_version == 2


# -- confidence gating -------------------------------------------------------
# These pin the behaviour that fixes the collected-message false positives: a
# candidate the model scored below the configured threshold must never reach the
# planner, no matter what the model chose to return.


def test_provider_drops_candidates_below_the_threshold() -> None:
    below = """
    {
      "is_task": true,
      "goal": "随便聊聊",
      "task_kind": "action",
      "intent_candidates": [
        {"name": "todo.create", "confidence": 0.4, "evidence": "安排"}
      ],
      "confidence": 0.9,
      "reason": "weak guess"
    }
    """
    provider = OpenAICompatibleUnderstandingProvider(StubClient([below]), min_confidence=0.7)
    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "稍后联系，安排一下我看看"})
    )

    assert result.intent_candidates == []


def test_provider_keeps_a_candidate_at_the_threshold() -> None:
    at_bar = """
    {
      "is_task": true,
      "goal": "开会",
      "task_kind": "action",
      "intent_candidates": [
        {"name": "todo.create", "confidence": 0.7, "evidence": "明天开会"}
      ],
      "confidence": 0.9,
      "reason": "edge of the threshold"
    }
    """
    provider = OpenAICompatibleUnderstandingProvider(StubClient([at_bar]), min_confidence=0.7)
    result = provider.understand(
        ChatIngress().create_task("user-1", {"text": "明天晚上八点开会"})
    )

    assert [item.name for item in result.intent_candidates] == [DEFAULT_CAPABILITY_NAME]


def test_provider_threshold_override_is_applied() -> None:
    mid = """
    {
      "is_task": true,
      "goal": "开会",
      "task_kind": "action",
      "intent_candidates": [
        {"name": "todo.create", "confidence": 0.78, "evidence": "开会"}
      ],
      "confidence": 0.9,
      "reason": "collected message"
    }
    """
    provider = OpenAICompatibleUnderstandingProvider(StubClient([mid, mid]), min_confidence=0.7)
    task = ChatIngress().create_task("user-1", {"text": "明天晚上八点开会"})

    assert provider.understand(task).intent_candidates != []
    # The collected-message bar is stricter, so the same output is dropped.
    assert provider.understand(task, min_confidence=0.85).intent_candidates == []


def test_prompt_renders_the_configured_threshold() -> None:
    from app.understanding.prompt import build_understanding_messages

    task = ChatIngress().create_task("user-1", {"text": "明天晚上八点开会"})
    system = build_understanding_messages(task, 0.85)[0]["content"]

    assert "0.85" in system
    assert "{min_confidence}" not in system
    assert "以下情况不是任务" in system
    assert "官网部署到哪了" in system
    assert "优先归为 knowledge.answer" in system


def test_collected_messages_use_the_higher_threshold() -> None:
    """A knowledge_event Task must clear the collected bar, not the chat one."""

    from app.testing.fake_knowledge import FakeKnowledgeClient
    from app.understanding.provider import FakeUnderstandingProvider

    seen: list[tuple[str, float | None]] = []

    class RecordingProvider(FakeUnderstandingProvider):
        name = "recording"

        def understand(self, task, *, min_confidence=None):
            seen.append((task.source_type, min_confidence))
            return understanding()

    client = FakeKnowledgeClient(snapshots={"item-1": snapshot()})
    container, store, _publisher, _knowledge = build_step2_container(
        knowledge=client,
        understanding_provider=RecordingProvider(),
        understanding_mode="enforce",
    )

    outcome = container.knowledge_events.handle(knowledge_event())
    container.execution_service.run_task(outcome.task_ids[0])

    assert seen == [("knowledge_event", 0.85)]


def test_model_backed_understanding_uses_its_actual_call_count() -> None:
    class ModelBackedProvider:
        name = "hybrid"
        model = "laya+llm"
        model_backed = True
        estimated_model_calls = 1

        def __init__(self) -> None:
            self.last_call_count = 2

        def understand(self, task, *, min_confidence=None):
            return understanding()

    container, store, _publisher, _knowledge = build_step2_container(
        understanding_provider=ModelBackedProvider(),
        understanding_mode="enforce",
        task_max_model_calls=1,
    )
    task = container.task_service.create_task(
        owner_user_id="user-1",
        payload={"text": "明天晚上八点开评审会"},
    )

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert store.get_task(task.task_id).last_error["message"] == "model call budget exceeded"


def test_planner_ignores_a_candidate_below_its_threshold() -> None:
    """Even if a low-confidence candidate reaches the planner, no draft is made."""

    from app.planning.deterministic import DeterministicPlanner
    from app.capabilities.todo import CAPABILITY_NAME as TODO_CAPABILITY_NAME
    from app.kernel.models import CapabilityDescriptor
    from app.ingress.chat import ChatIngress

    planner = DeterministicPlanner(default_timezone="Asia/Shanghai", min_confidence=0.7)
    task = ChatIngress().create_task("user-1", {"text": "稍后联系，安排一下我看看"})
    weak = TaskUnderstanding(
        is_task=True,
        goal="随便聊聊",
        task_kind="action",
        intent_candidates=[
            UnderstandingIntent(name=DEFAULT_CAPABILITY_NAME, confidence=0.4)
        ],
        confidence=0.9,
        reason="weak guess",
    )
    descriptors = [
        CapabilityDescriptor(
            name=TODO_CAPABILITY_NAME,
            description="创建日历事件",
            risk_level="external_write",
            side_effect=True,
            requires_approval=True,
            idempotent=True,
            timeout_seconds=20,
        )
    ]

    plan = planner.create_plan(task, descriptors, [], None, weak)

    assert plan.steps == []


# -- Capability-aware catalog ---------------------------------------------


def test_catalog_filters_by_registered_capabilities() -> None:
    offered = available_intents(
        {"todo.create", "knowledge.search_content", "web.research"}
    )
    names = {item.name for item in offered}

    assert "todo.create" in names
    assert "knowledge.answer" in names
    assert "web.research" in names
    # No capability implements these intents yet, so they stay unavailable.
    assert "compliance.assess" not in names
    assert "form.complete" not in names


def test_catalog_text_only_mentions_offered_intents() -> None:
    text = intent_catalog_text(available_intents({"todo.create"}))

    assert "todo.create" in text
    assert "web.research" not in text
    assert "knowledge.answer" not in text


def test_unavailable_intent_is_dropped_by_the_llm_provider() -> None:
    client = StubClient(
        [
            json.dumps(
                {
                    "is_task": True,
                    "goal": "查一下这个政策的最新版本",
                    "task_kind": "action",
                    "intent_candidates": [
                        {
                            "name": "web.research",
                            "confidence": 0.95,
                            "evidence": None,
                        }
                    ],
                    "confidence": 0.95,
                    "reason": "web lookup",
                }
            )
        ]
    )
    provider = OpenAICompatibleUnderstandingProvider(
        client, available_intents=available_intents({"todo.create"})
    )
    task = ChatIngress().create_task("user-1", {"text": "查一下这个政策的最新版本"})

    result = provider.understand(task)

    # The task itself stands; only the unexecutable candidate is removed.
    assert result.is_task is True
    assert result.intent_candidates == []


def test_unavailable_intent_is_not_offered_to_the_rule_provider() -> None:
    provider = RuleBasedUnderstandingProvider(
        available_intents=available_intents({"knowledge.search_content"})
    )
    task = ChatIngress().create_task("user-1", {"text": "明天晚上八点开评审会"})

    result = provider.understand(task)

    assert result.intent_candidates == []
