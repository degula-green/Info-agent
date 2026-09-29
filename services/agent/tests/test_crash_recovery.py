"""Crash recovery, re-plan idempotency and approval-binding regression tests.

These pin the Step-3 defects found in review:

* a worker dying mid-Step must not skip that Step external write;
* resuming must not re-issue a Call that already happened;
* an approval must not authorise Step arguments rewritten by a re-plan;
* the per-Step attempt budget must survive replanning.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.kernel.approval import ApprovalError, arguments_fingerprint
from app.kernel.errors import RetryableCapabilityError
from app.kernel.models import CapabilityDescriptor, Plan, PlanStep, PlannerDecision
from tests.support import build_step2_container, build_test_container, create_task


class CountingReadCapability:
    descriptor = CapabilityDescriptor(
        name="test.read",
        description="Counting read used to observe re-execution.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"value": arguments.get("value"), "calls": self.calls}


def _task_with_steps(store, task_id: str, specs: list[tuple[str, dict, str]]):
    """Persist a Plan whose Steps start in the given statuses."""

    plan_id = str(uuid.uuid4())
    steps = [
        PlanStep(
            step_id=f"{plan_id}-step-{index}",
            plan_id=plan_id,
            order=index,
            capability=capability,
            arguments=dict(arguments),
            status=status,
        )
        for index, (capability, arguments, status) in enumerate(specs, start=1)
    ]
    plan = Plan(plan_id=plan_id, task_id=task_id, objective="resume", status="running")
    store.save_plan(plan)
    store.save_steps(task_id, steps)
    record = store.get_task(task_id)
    record.status = "executing"
    record.current_plan_id = plan_id
    store.commit(record)
    return plan_id, steps


def test_resume_does_not_skip_a_step_the_worker_died_in_mid_call() -> None:
    """A Step left "running" is unfinished work, not finished work.

    Before the fix the kernel jumped to the next pending Step, found none, asked
    the Planner, and failed the Task with
    "planner returned continue with no pending step" while the external effect
    never happened.
    """

    counting = CountingReadCapability()
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[counting]
    )
    task = create_task(container)
    plan_id, steps = _task_with_steps(
        store, task.task_id, [("test.read", {"value": "a"}, "running")]
    )
    store.update_step(store.get_step(steps[0].step_id), attempt_count=1)

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert counting.calls == 1
    assert store.get_step(steps[0].step_id).status == "succeeded"


def test_resume_does_not_repeat_a_call_that_already_happened() -> None:
    """When the Call row says succeeded, resuming settles the Step instead.

    This is the case that used to write the same external effect twice.
    """

    counting = CountingReadCapability()
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[counting]
    )
    task = create_task(
        container,
        steps=[
            {"capability": "test.read", "arguments": {"value": "a"}},
            {"capability": "test.read", "arguments": {"value": "b"}},
        ],
    )
    assert container.execution_service.run_task(task.task_id) == "succeeded"
    plan_id = store.get_task(task.task_id).current_plan_id
    assert counting.calls == 2

    # Rewind the first Step to the shape a crash leaves behind.
    first = store.get_step(store.list_steps(plan_id)[0].step_id)
    first.status = "running"
    store.update_step(first)
    record = store.get_task(task.task_id)
    record.status = "executing"
    store.commit(record)

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert counting.calls == 2, "a persisted successful call must not be re-issued"


class SameIdReplanner:
    """Re-plans a failed Step while deliberately reusing its ``step_id``."""

    name = "deterministic"
    last_call_count = 1

    def __init__(self, *, capability: str, first: dict, replacement: dict) -> None:
        self.capability = capability
        self.first = dict(first)
        self.replacement = dict(replacement)
        self.plan_id = str(uuid.uuid4())
        self.step_id = f"{self.plan_id}-step-1"

    def _plan(self, task_id: str, arguments: dict) -> Plan:
        return Plan(
            plan_id=self.plan_id,
            task_id=task_id,
            objective="same step id",
            steps=[
                PlanStep(
                    step_id=self.step_id,
                    plan_id=self.plan_id,
                    order=1,
                    capability=self.capability,
                    arguments=dict(arguments),
                )
            ],
        )

    def create_plan(self, task, capabilities, observations, constraints, understanding=None):
        return self._plan(task.task_id, self.first)

    def decide_after_observation(self, task, current_plan, observations, constraints, understanding=None):
        latest = observations[-1] if observations else None
        if latest is None or latest.status == "succeeded":
            if any(step.status in {"pending", "ready", "running"} for step in current_plan.steps):
                return PlannerDecision(action="continue")
            return PlannerDecision(action="complete")
        return PlannerDecision(
            action="replan",
            plan=self._plan(task.task_id, self.replacement),
            reason="retry with rewritten arguments and the same step id",
        )


def test_replanning_cannot_inherit_an_old_approval() -> None:
    """Rewriting a Step under an approved step_id must require a new approval.

    Both halves are checked: the gateway refuses the stale approval, and the
    runtime does not treat it as "already approved".
    """

    container, store, _publisher, _registry = build_test_container()
    planner = SameIdReplanner(
        capability="fake.write",
        first={"value": "approved"},
        replacement={"value": "rewritten by the planner"},
    )
    container.execution_service.runtime.planner = planner
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    approval = store.list_approvals(task_id=task.task_id)[0]
    assert approval.arguments_hash == arguments_fingerprint({"value": "approved"})

    # The user approves; then the Step is rewritten in place (same step_id).
    container.execution_service.approval_gateway.decide(
        approval.approval_id, owner_user_id="user-1", approve=True
    )
    rewritten = store.get_step(planner.step_id)
    rewritten.arguments = {"value": "rewritten after approval"}
    store.update_step(rewritten)

    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    assert rewritten.arguments != approval.arguments


def test_stale_approval_is_rejected_by_the_gateway() -> None:
    """The gateway itself refuses an approval whose arguments changed."""

    container, store, _publisher, _knowledge = build_step2_container()
    task = container.task_service.create_task(
        owner_user_id="user-1", payload={"text": "明天晚上八点开会"}
    )
    assert container.execution_service.run_task(task.task_id) == "waiting_approval"
    pending = store.list_approvals(task_id=task.task_id)[0]

    step = store.get_step(pending.step_id)
    step.arguments = dict(step.arguments, title="被改写的标题")
    store.update_step(step)

    with pytest.raises(ApprovalError):
        container.execution_service.approval_gateway.decide(
            pending.approval_id, owner_user_id="user-1", approve=True
        )
    assert store.get_approval(pending.approval_id).status == "superseded"


def test_attempt_budget_survives_replanning() -> None:
    """A re-planned retry keeps the attempts already spent on the effect."""

    attempts = {"n": 0}

    class AlwaysFlaky:
        descriptor = CapabilityDescriptor(
            name="test.flaky",
            description="Always fails retryably.",
            risk_level="read_only",
            side_effect=False,
            requires_approval=False,
            idempotent=False,
            timeout_seconds=5,
        )

        def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
            return dict(arguments)

        def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
            attempts["n"] += 1
            raise RetryableCapabilityError("always fails")

    class FreshPlanReplanner(SameIdReplanner):
        """Every re-plan mints a new plan_id, as a real Planner would."""

        def decide_after_observation(self, task, current_plan, observations, constraints, understanding=None):
            self.plan_id = str(uuid.uuid4())
            self.step_id = f"{self.plan_id}-step-1"
            return super().decide_after_observation(
                task, current_plan, observations, constraints, understanding
            )

    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[AlwaysFlaky()],
        planner=FreshPlanReplanner(
            capability="test.flaky", first={}, replacement={}
        ),
        task_max_replans=6,
        task_max_retries=2,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert attempts["n"] <= 2, "replanning must not reset the per-step attempt budget"
