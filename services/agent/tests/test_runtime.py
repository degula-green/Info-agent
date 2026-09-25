"""Step-1 execution kernel tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.kernel.errors import (
    InvalidStateTransitionError,
    PermanentCapabilityError,
    RetryableCapabilityError,
    UnknownExternalResultError,
)
from app.kernel.limits import ExecutionLimits
from app.kernel.models import CapabilityDescriptor
from app.kernel.runtime import AgentRuntime
from app.kernel.states import ensure_task_transition
from tests.support import (
    CountingReadCapability,
    ValueInputPayload,
    build_test_container,
    create_task,
    event_types,
)


class FlakyCapability:
    descriptor = CapabilityDescriptor(
        name="test.flaky",
        description="Fails once with a retryable error, then succeeds.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict) -> ValueInputPayload:
        return ValueInputPayload.model_validate({"value": arguments.get("value") or "x"})

    def execute(self, arguments: ValueInputPayload) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RetryableCapabilityError("temporary upstream failure")
        return {"calls": self.calls}


class PermanentFailCapability:
    descriptor = CapabilityDescriptor(
        name="test.permanent",
        description="Always fails permanently.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=False,
        timeout_seconds=5,
    )

    def validate(self, arguments: dict) -> ValueInputPayload:
        return ValueInputPayload.model_validate({"value": arguments.get("value") or "x"})

    def execute(self, arguments: ValueInputPayload) -> dict:
        raise PermanentCapabilityError("invalid request")


class UnknownResultCapability:
    descriptor = CapabilityDescriptor(
        name="test.unknown",
        description="External call with an undetermined result.",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=False,
        timeout_seconds=5,
    )

    def validate(self, arguments: dict) -> ValueInputPayload:
        return ValueInputPayload.model_validate({"value": arguments.get("value") or "x"})

    def execute(self, arguments: ValueInputPayload) -> dict:
        raise UnknownExternalResultError("gateway timeout after submit")


def test_sequential_read_task_completes_and_records_events() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(
        container,
        steps=[
            {"capability": "fake.read", "arguments": {"value": "first"}},
            {"capability": "fake.read", "arguments": {"value": "second"}},
        ],
    )

    status = container.execution_service.run_task(task.task_id)

    assert status == "succeeded"
    observations = store.list_observations(task.task_id)
    assert [item.output["value"] for item in observations] == ["first", "second"]
    types = event_types(store, task.task_id)
    assert types[0] == "task.accepted"
    assert types.count("step.started") == 2
    assert types.count("step.succeeded") == 2
    assert types[-1] == "task.completed"
    sequences = [event.sequence for event in store.list_events(task.task_id)]
    assert sequences == sorted(sequences) == list(range(1, len(sequences) + 1))
    checkpoint = store.get_task(task.task_id).checkpoint
    assert checkpoint["next_step_id"] is None


def test_steps_execute_in_order() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(
        container,
        steps=[
            {"capability": "fake.read", "arguments": {"value": "one"}},
            {"capability": "fake.read", "arguments": {"value": "two"}},
            {"capability": "fake.read", "arguments": {"value": "three"}},
        ],
    )
    container.execution_service.run_task(task.task_id)
    started = [
        event.payload["step_id"]
        for event in store.list_events(task.task_id)
        if event.event_type == "step.started"
    ]
    plan = store.get_active_plan(task.task_id)
    ordered = [step.step_id for step in sorted(plan.steps, key=lambda item: item.order)]
    assert started == ordered


def test_write_capability_waits_for_approval_then_resumes() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(
        container,
        steps=[{"capability": "fake.write", "arguments": {"value": "danger"}}],
    )

    status = container.execution_service.run_task(task.task_id)
    assert status == "waiting_approval"
    assert store.get_task(task.task_id).status == "waiting_approval"
    assert store.list_observations(task.task_id) == []
    assert "task.waiting_approval" in event_types(store, task.task_id)

    approval = store.list_approvals(task_id=task.task_id)[0]
    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=True, version=approval.version
    )
    assert store.get_task(task.task_id).status == "ready"

    status = container.execution_service.run_task(task.task_id)
    assert status == "succeeded"
    assert len(store.list_observations(task.task_id)) == 1
    assert "approval.approved" in event_types(store, task.task_id)


def test_rejected_approval_fails_the_task() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(
        container,
        steps=[{"capability": "fake.write", "arguments": {"value": "danger"}}],
    )
    container.execution_service.run_task(task.task_id)
    approval = store.list_approvals(task_id=task.task_id)[0]

    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=False
    )

    assert store.get_task(task.task_id).status == "failed"
    assert "approval.rejected" in event_types(store, task.task_id)


def test_approval_rejects_wrong_owner_and_replayed_decision() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(
        container,
        steps=[{"capability": "fake.write", "arguments": {"value": "danger"}}],
    )
    container.execution_service.run_task(task.task_id)
    approval = store.list_approvals(task_id=task.task_id)[0]

    with pytest.raises(Exception):
        container.execution_service.approval_gateway.decide(
            approval.approval_id, owner_user_id="someone-else", approve=True
        )

    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=True
    )
    with pytest.raises(Exception):
        container.execution_service.approval_gateway.decide(
            approval.approval_id, owner_user_id="user-1", approve=True
        )


def test_task_pauses_for_user_input_and_replans_after_input() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.ask", "arguments": {}}])

    status = container.execution_service.run_task(task.task_id)

    assert status == "waiting_input"
    assert "task.waiting_input" in event_types(store, task.task_id)

    updated = container.task_service.submit_input(
        task.task_id, owner_user_id="user-1", payload={"text": "now I provide the value"}
    )
    assert updated.status == "planning"
    assert store.get_active_plan(task.task_id) is None
    assert len(store.list_inputs(task.task_id)) == 2


def test_retryable_error_is_retried_with_same_idempotency_key() -> None:
    flaky = FlakyCapability()
    container, store, _publisher, _registry = build_test_container(extra_capabilities=[flaky])
    task = create_task(container, steps=[{"capability": "test.flaky", "arguments": {"value": "x"}}])

    status = container.execution_service.run_task(task.task_id)

    assert status == "succeeded"
    assert flaky.calls == 2
    assert "task.retrying" in event_types(store, task.task_id)
    # One Step keeps one CapabilityCall: the retry reuses the same idempotency
    # key and therefore the same external request id.
    calls = list(store.calls.values())
    assert len(calls) == 1
    assert calls[0].attempt == 2
    assert calls[0].idempotency_key == f"{task.task_id}|{calls[0].plan_id}|{calls[0].step_id}"


def test_permanent_error_fails_task_without_retry() -> None:
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[PermanentFailCapability()]
    )
    task = create_task(
        container, steps=[{"capability": "test.permanent", "arguments": {"value": "x"}}]
    )

    status = container.execution_service.run_task(task.task_id)

    assert status == "failed"
    assert store.get_task(task.task_id).last_error["classification"] == "permanent_error"
    assert "step.failed" in event_types(store, task.task_id)


def test_unknown_external_result_is_not_retried() -> None:
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[UnknownResultCapability()]
    )
    task = create_task(
        container, steps=[{"capability": "test.unknown", "arguments": {"value": "x"}}]
    )

    status = container.execution_service.run_task(task.task_id)
    assert status == "waiting_approval"

    approval = store.list_approvals(task_id=task.task_id)[0]
    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=True
    )
    status = container.execution_service.run_task(task.task_id)

    assert status == "unknown"
    assert store.get_task(task.task_id).last_error["classification"] == "unknown_external_result"
    assert "task.retrying" not in event_types(store, task.task_id)


def test_duplicate_wakeup_does_not_execute_step_twice() -> None:
    counting = CountingReadCapability()
    container, store, _publisher, _registry = build_test_container(extra_capabilities=[counting])
    task = create_task(container, steps=[{"capability": "test.read", "arguments": {"value": "a"}}])

    container.execution_service.run_task(task.task_id)
    container.execution_service.run_task(task.task_id)
    container.execution_service.run_task(task.task_id)

    assert counting.calls == 1
    assert len(store.list_observations(task.task_id)) == 1


def test_restart_resumes_from_persisted_state_without_reexecution() -> None:
    counting = CountingReadCapability()
    container, store, _publisher, _registry = build_test_container(extra_capabilities=[counting])
    task = create_task(
        container,
        steps=[
            {"capability": "test.read", "arguments": {"value": "step-one"}},
            {"capability": "fake.write", "arguments": {"value": "step-two"}},
        ],
    )
    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    assert counting.calls == 1

    # A new service instance over the same store models a worker restart.
    from app.application.execution_service import ExecutionService

    restarted = ExecutionService(
        store=store,
        registry=container.registry,
        planner=container.planner,
        policy=container.policy,
        publisher=_publisher,
        settings=container.settings,
    )
    approval = store.list_approvals(task_id=task.task_id)[0]
    restarted.approval_gateway.decide(approval.approval_id, owner_user_id="user-1", approve=True)

    assert restarted.run_task(task.task_id) == "succeeded"
    assert counting.calls == 1
    assert len(store.list_observations(task.task_id)) == 2


def test_outbox_recovers_after_publish_failure() -> None:
    from app.infrastructure.redis.streams import OutboxDispatcher
    from app.testing.fake_publisher import FakeTaskPublisher

    container, store, _publisher, _registry = build_test_container()
    failing = FakeTaskPublisher(fail_times=1)
    dispatcher = OutboxDispatcher(store, failing, batch_size=10)

    task = create_task(container)
    assert dispatcher.dispatch_once() == 0
    assert store.pending_outbox()

    assert dispatcher.dispatch_once() == 1
    assert store.pending_outbox() == []
    assert failing.task_ids() == [task.task_id]


def test_resume_unfinished_tasks_requeues_wakeups() -> None:
    container, store, publisher, _registry = build_test_container()
    task = create_task(container)
    container.execution_service.dispatch_outbox()
    publisher.published.clear()

    recovered = container.execution_service.resume_unfinished_tasks()
    assert recovered == 1
    container.execution_service.dispatch_outbox()
    assert publisher.task_ids() == [task.task_id]


def test_plan_budget_is_enforced() -> None:
    container, store, _publisher, _registry = build_test_container(task_max_steps=1)
    task = create_task(
        container,
        steps=[
            {"capability": "fake.read", "arguments": {"value": "a"}},
            {"capability": "fake.read", "arguments": {"value": "b"}},
        ],
    )

    status = container.execution_service.run_task(task.task_id)

    assert status == "failed"
    assert store.get_task(task.task_id).last_error["classification"] == "validation_error"


def test_invalid_state_transition_is_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_task_transition("succeeded", "executing")


def test_execution_limits_backoff_is_capped() -> None:
    limits = ExecutionLimits(retry_backoff_seconds=[1.0, 5.0, 20.0])
    assert limits.backoff_for(1) == 1.0
    assert limits.backoff_for(2) == 5.0
    assert limits.backoff_for(3) == 20.0
    assert limits.backoff_for(9) == 20.0


def test_task_lease_prevents_two_workers_running_the_same_step() -> None:
    container, store, _publisher, _registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.read", "arguments": {"value": "a"}}])

    assert store.acquire_lease(task.task_id, "worker-a", 60) is True
    assert store.acquire_lease(task.task_id, "worker-b", 60) is False

    result = container.execution_service.runtime.run_task(task.task_id, lease_owner="worker-b")
    assert result.waiting_for == "lease"
    assert store.get_task(task.task_id).status == "received"

    store.release_lease(task.task_id, "worker-a")
    assert container.execution_service.run_task(task.task_id) == "succeeded"


def test_waiting_for_approval_does_not_consume_the_execution_budget() -> None:
    """A human may take longer than the budget to confirm without failing the Task."""

    container, store, _publisher, _registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.write", "arguments": {"value": "x"}}])
    assert container.execution_service.run_task(task.task_id) == "waiting_approval"

    # The Task entered waiting_approval well before the user confirmed it.
    record = store.get_task(task.task_id)
    record.created_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    store.commit(record)

    approval = store.list_approvals(task_id=task.task_id)[0]
    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=True
    )

    assert container.execution_service.run_task(task.task_id) == "succeeded"


def test_execution_budget_still_bounds_a_single_drive() -> None:
    container, store, _publisher, registry = build_test_container()
    task = create_task(container, steps=[{"capability": "fake.read", "arguments": {"value": "a"}}])

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = iter([0.0, 400.0])
    runtime = AgentRuntime(
        store=store,
        registry=registry,
        planner=container.planner,
        policy=container.policy,
        clock=lambda: base + timedelta(seconds=next(ticks, 400.0)),
    )

    result = runtime.run_task(task.task_id)

    assert result.status == "failed"
    error = store.get_task(task.task_id).last_error
    assert error["message"] == "execution time limit exceeded"


def test_execution_service_uses_a_process_specific_lease_owner() -> None:
    """A fixed lease owner would let the API race the worker process."""

    import os

    from app.application.execution_service import ExecutionService

    container, store, publisher, _registry = build_test_container()
    default_service = ExecutionService(
        store=store,
        registry=container.registry,
        planner=container.planner,
        policy=container.policy,
        publisher=publisher,
        settings=container.settings,
    )
    explicit = ExecutionService(
        store=store,
        registry=container.registry,
        planner=container.planner,
        policy=container.policy,
        publisher=publisher,
        settings=container.settings,
        lease_owner="worker-1",
    )

    assert default_service.lease_owner != "worker"
    assert str(os.getpid()) in default_service.lease_owner
    assert explicit.lease_owner != default_service.lease_owner
