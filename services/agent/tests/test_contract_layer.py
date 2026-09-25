"""Contract-layer acceptance tests."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.ingress.chat import ChatIngress
from app.ingress.knowledge_events import KnowledgeEventIngress
from app.kernel.errors import CapabilityNotFoundError, ContractValidationError
from app.kernel.models import Plan, PlanStep, TaskEnvelope
from app.kernel.registry import CapabilityRegistry
from app.kernel.validator import PlanValidator
from app.testing.fake_capabilities import FakeReadCapability, FakeWriteCapability
from app.testing.fake_planner import FakePlanner, FakeTaskUnderstandingProvider
from app.testing.fake_policy import FakePolicy
from app.testing.harness import FakeHarness
from app.testing.in_memory_store import InMemoryTaskStore


def make_task(task_id: str = "task-1") -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        source_type="chat",
        owner_user_id="user-1",
        input={"text": "read value"},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def make_registry() -> CapabilityRegistry:
    return CapabilityRegistry([FakeReadCapability(), FakeWriteCapability()])


def test_chat_ingress_creates_task_envelope() -> None:
    task = ChatIngress().create_task("user-1", {"text": "find this"}, task_id="task-chat")
    assert task.task_id == "task-chat"
    assert task.source_type == "chat"
    assert task.owner_user_id == "user-1"
    assert task.input == {"text": "find this"}


def knowledge_event(**payload: object) -> dict:
    return {"event_type": "knowledge.ready", "event_id": "event-1", "payload": dict(payload)}


def knowledge_snapshot(**overrides: object) -> dict:
    snapshot = {
        "conversation_ingestion_id": "ingestion-1",
        "conversation_type": "private",
        "platform": "feishu",
        "source_message_id": "message-1",
        "message_type": "text",
        "text": "今天晚上八点开会",
        "content_version": 1,
        "acl_version": 3,
        "eligible_owners": [{"owner_user_id": "user-1", "reason_code": None}],
    }
    snapshot.update(overrides)
    return snapshot


def test_knowledge_event_ingress_creates_one_task_for_a_private_chat() -> None:
    tasks = KnowledgeEventIngress().create_tasks(knowledge_event(), knowledge_snapshot())
    assert len(tasks) == 1
    task = tasks[0]
    assert task.source_type == "knowledge_event"
    assert task.owner_user_id == "user-1"
    assert task.input["text"] == "今天晚上八点开会"
    assert task.source_ref == {
        "event_id": "event-1",
        "source_message_id": "message-1",
        "platform": "feishu",
        "conversation_ingestion_id": "ingestion-1",
        "conversation_type": "private",
        "content_version": 1,
        "acl_version": 3,
    }


def test_knowledge_event_ingress_fans_out_to_every_eligible_member() -> None:
    snapshot = knowledge_snapshot(
        conversation_type="group",
        eligible_owners=[
            {"owner_user_id": "user-1", "reason_code": None},
            {"owner_user_id": "user-2", "reason_code": None},
            {"owner_user_id": "user-1", "reason_code": None},
            {"owner_user_id": "", "reason_code": None},
        ],
        excluded_members=[
            {"external_user_id": "ou_1", "reason_code": "identity_conflict"},
            {"external_user_id": "ou_2", "reason_code": "not_a_member"},
        ],
    )
    tasks = KnowledgeEventIngress().create_tasks(knowledge_event(), snapshot)
    assert [task.owner_user_id for task in tasks] == ["user-1", "user-2"]
    assert len({task.task_id for task in tasks}) == 2
    assert all(task.source_ref["conversation_type"] == "group" for task in tasks)


def test_knowledge_event_ingress_creates_nothing_without_an_eligible_owner() -> None:
    snapshot = knowledge_snapshot(eligible_owners=[])
    assert KnowledgeEventIngress().create_tasks(knowledge_event(), snapshot) == []


def test_knowledge_event_ingress_rejects_wrong_event_platform_type_and_text() -> None:
    ingress = KnowledgeEventIngress()
    assert ingress.is_candidate({"event_type": "other"}, knowledge_snapshot()) is False
    assert ingress.is_candidate(knowledge_event(), knowledge_snapshot(platform="dingtalk")) is False
    assert ingress.is_candidate(knowledge_event(), knowledge_snapshot(message_type="image")) is False
    assert ingress.is_candidate(knowledge_event(), knowledge_snapshot(source_message_id="")) is False
    assert ingress.is_candidate(knowledge_event(), knowledge_snapshot(text="哈哈哈")) is False


def test_knowledge_event_ingress_accepts_every_whitelisted_platform() -> None:
    ingress = KnowledgeEventIngress()
    for platform in ("feishu", "wecom", "wechat", "Feishu"):
        assert ingress.is_candidate(knowledge_event(), knowledge_snapshot(platform=platform))
    configured = KnowledgeEventIngress(platforms=["feishu"])
    assert configured.is_candidate(knowledge_event(), knowledge_snapshot(platform="feishu"))
    assert not configured.is_candidate(knowledge_event(), knowledge_snapshot(platform="wechat"))


def test_knowledge_event_ingress_schedule_hint_and_idempotency_key() -> None:
    ingress = KnowledgeEventIngress()
    assert ingress.schedule_hint("明天下午三点评审") is True
    assert ingress.schedule_hint("约个时间碰头") is True
    assert ingress.schedule_hint("收到，我看一下") is False
    assert ingress.schedule_hint(None) is False
    assert KnowledgeEventIngress.idempotency_key("message-1", 2, "user-1") == (
        "knowledge_event:user-1:message-1:2"
    )


def test_registry_exposes_trusted_descriptors_and_rejects_unknown() -> None:
    registry = make_registry()
    assert {item.name for item in registry.list_descriptors()} == {"fake.read", "fake.write"}
    assert registry.find("missing") is None
    with pytest.raises(CapabilityNotFoundError, match="unknown capability"):
        registry.get("missing")


def test_validator_accepts_valid_plan_and_rejects_invalid_arguments() -> None:
    registry = make_registry()
    validator = PlanValidator(registry)
    plan = Plan(
        plan_id="plan-1",
        task_id="task-1",
        objective="read",
        steps=[PlanStep(step_id="step-1", plan_id="plan-1", order=1, capability="fake.read", arguments={"value": "ok"})],
    )
    validator.validate(plan, make_task())

    invalid = plan.model_copy(deep=True)
    invalid.steps[0].arguments = {"value": ""}
    with pytest.raises(ContractValidationError, match="invalid arguments"):
        validator.validate(invalid, make_task())


def test_validator_rejects_unknown_duplicate_and_wrong_order_or_ownership() -> None:
    registry = make_registry()
    validator = PlanValidator(registry)
    plan = Plan(
        plan_id="plan-1",
        task_id="task-1",
        objective="bad",
        steps=[
            PlanStep(step_id="same", plan_id="plan-1", order=2, capability="missing", arguments={}),
            PlanStep(step_id="same", plan_id="other-plan", order=3, capability="fake.read", arguments={"value": "ok"}),
        ],
    )
    with pytest.raises(ContractValidationError) as exc_info:
        validator.validate(plan, make_task())
    message = " ".join(exc_info.value.errors)
    assert "unknown capability" in message
    assert "duplicate step_id" in message
    assert "ordered sequentially" in message
    assert "does not belong" in message


def test_policy_allows_read_requires_approval_for_write_and_denies_unknown() -> None:
    registry = make_registry()
    policy = FakePolicy(registry)
    task = make_task()
    read_step = PlanStep(step_id="read", plan_id="plan", order=1, capability="fake.read", arguments={"value": "ok"})
    write_step = read_step.model_copy(update={"step_id": "write", "capability": "fake.write"})
    unknown_step = read_step.model_copy(update={"step_id": "unknown", "capability": "missing"})
    assert policy.evaluate(task, read_step, registry.get("fake.read").descriptor).action == "allow"
    assert policy.evaluate(task, write_step, registry.get("fake.write").descriptor).action == "require_approval"
    assert policy.evaluate(task, unknown_step, None).action == "deny"


def test_policy_uses_registry_descriptor_instead_of_planner_metadata() -> None:
    registry = make_registry()
    policy = FakePolicy(registry)
    task = make_task()
    step = PlanStep(step_id="read", plan_id="plan", order=1, capability="fake.read", arguments={"value": "ok"})
    forged = registry.get("fake.write").descriptor
    assert policy.evaluate(task, step, forged).action == "allow"


def test_harness_runs_sequential_reads_and_persists_observations() -> None:
    registry = CapabilityRegistry([FakeReadCapability()])
    store = InMemoryTaskStore()
    harness = FakeHarness(
        registry=registry,
        planner=FakePlanner([
            {"capability": "fake.read", "arguments": {"value": "first"}},
            {"capability": "fake.read", "arguments": {"value": "second"}},
        ]),
        policy=FakePolicy(registry),
        store=store,
        understanding_provider=FakeTaskUnderstandingProvider(),
    )
    result = harness.run(make_task())
    assert result.plan.status == "succeeded"
    assert result.understanding is not None
    assert result.understanding.goal == "read value"
    assert result.executed_steps == ["step-1", "step-2"]
    assert [item.output["value"] for item in store.list_for_task("task-1")] == ["first", "second"]
    assert store.get_task("task-1") is not None
    assert store.get_plan(result.plan.plan_id).status == "succeeded"


def test_harness_pauses_write_for_approval_without_execution() -> None:
    write = FakeWriteCapability()
    registry = CapabilityRegistry([write])
    harness = FakeHarness(
        registry=registry,
        planner=FakePlanner([{"capability": "fake.write", "arguments": {"value": "danger"}}]),
        policy=FakePolicy(registry),
        store=InMemoryTaskStore(),
    )
    result = harness.run(make_task())
    assert result.plan.status == "waiting_approval"
    assert result.decisions[0].action == "require_approval"
    assert result.executed_steps == []
    assert write.execute_count == 0
    assert result.observations == []
    assert result.approval_request is not None
    assert result.approval_request.capability == "fake.write"


def test_model_rejects_invalid_source_type_and_confidence() -> None:
    with pytest.raises(ValidationError):
        TaskEnvelope(
            task_id="task",
            source_type="email",
            owner_user_id="user",
            input={},
            created_at=datetime.now(timezone.utc),
        )
