"""Dynamic planning, reference resolution and unsupported-intent tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.kernel.errors import (
    PermanentCapabilityError,
    RetryableCapabilityError,
    UnknownExternalResultError,
)
from app.kernel.models import (
    CapabilityDescriptor,
    Observation,
    Plan,
    PlannerDecision,
    PlanningConstraints,
)
from app.planning.llm import OpenAICompatiblePlanner
from app.kernel.references import resolve_arguments
from app.testing.fake_planner import FakePlanner, FakeReplanner
from tests.support import build_test_container, create_task, event_types


class FlakyOnceCapability:
    descriptor = CapabilityDescriptor(
        name="test.flaky",
        description="Fail once with a retryable error, then succeed.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": str(arguments.get("value") or "x")}

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise RetryableCapabilityError("temporary failure")
        return {"value": arguments["value"], "calls": self.calls}


class UnknownCapability:
    descriptor = CapabilityDescriptor(
        name="test.unknown",
        description="Return an uncertain external result.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=False,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise UnknownExternalResultError("timeout after request was sent")


class DecisionPlanner:
    def __init__(self, decision: PlannerDecision) -> None:
        self.decision = decision

    def create_plan(
        self,
        task,
        capabilities,
        observations,
        constraints: PlanningConstraints,
        understanding=None,
    ) -> Plan:
        return Plan(
            plan_id=str(uuid4()),
            task_id=task.task_id,
            objective="unsupported test",
            steps=[],
        )

    def decide_after_observation(
        self,
        task,
        current_plan,
        observations,
        constraints: PlanningConstraints,
        understanding=None,
    ) -> PlannerDecision:
        return self.decision


class StubPlannerClient:
    model = "stub-planner"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0
        self.last_call_count = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.last_call_count = 1
        return self.outputs.pop(0)


def test_step_arguments_resolve_previous_observation() -> None:
    planner = FakePlanner(
        [
            {"capability": "fake.read", "arguments": {"value": "first"}},
            {
                "capability": "fake.read",
                "arguments": {"value": "$steps.step-1.output.value"},
            },
        ]
    )
    container, store, _publisher, _registry = build_test_container(planner=planner)
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    observations = store.list_observations(task.task_id)
    assert [item.output["value"] for item in observations] == ["first", "first"]


def test_cross_plan_reference_uses_observation_id() -> None:
    observation = Observation(
        observation_id="obs-1",
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        capability="fake.read",
        status="succeeded",
        output={"nested": {"value": "from-observation"}},
        created_at=datetime.now(timezone.utc),
    )

    resolved = resolve_arguments(
        {"value": "$observations.obs-1.output.nested.value"},
        [observation],
    )

    assert resolved == {"value": "from-observation"}


def test_a_retryable_failure_is_absorbed_by_the_in_place_retry() -> None:
    """The retry succeeds, so no new Plan version is minted.

    Retrying used to be expressed as a re-plan. It now happens inside the same
    Step, which is why the Plan version stays at 1 and nothing is marked as
    replaced.
    """

    flaky = FlakyOnceCapability()
    replanner = FakeReplanner(
        initial_steps=[{"capability": "test.flaky", "arguments": {"value": "x"}}],
        replan_steps=[{"capability": "test.flaky", "arguments": {"value": "x"}}],
    )
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[flaky],
        planner=replanner,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert stored.replan_count == 0
    assert stored.current_plan_version == 1
    assert flaky.calls == 2
    assert "plan.replanned" not in event_types(store, task.task_id)


def test_a_replan_happens_once_the_retry_budget_is_spent() -> None:
    """Retries are bounded: the planner only gets a say after they run out."""

    class AlwaysRetryable:
        descriptor = CapabilityDescriptor(
            name="test.flaky",
            description="Always fails with a retryable error.",
            risk_level="read_only",
            side_effect=False,
            requires_approval=False,
            idempotent=True,
            timeout_seconds=5,
        )

        def __init__(self) -> None:
            self.calls = 0

        def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"value": str(arguments.get("value") or "x")}

        def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            raise RetryableCapabilityError("always fails")

    flaky = AlwaysRetryable()
    replanner = FakeReplanner(
        initial_steps=[{"capability": "test.flaky", "arguments": {"value": "x"}}],
        replan_steps=[{"capability": "test.flaky", "arguments": {"value": "y"}}],
        trigger_classifications=("retryable_error",),
    )
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[flaky],
        planner=replanner,
        task_max_retries=3,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "failed"

    assert flaky.calls == 3, "the retry budget bounds the calls before any re-plan"
    assert "planner.decision" in event_types(store, task.task_id)
    assert "plan.replanned" in event_types(store, task.task_id)


def test_first_retryable_failure_never_reaches_the_planner() -> None:
    """The Plan is still valid — only the call failed — so no new version is made.

    One ``planner.decision`` remains, and it belongs to the Step that succeeded,
    not to the failure.
    """

    flaky = FlakyOnceCapability()
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[flaky],
        planner=FakePlanner(
            [{"capability": "test.flaky", "arguments": {"value": "x"}}]
        ),
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    assert flaky.calls == 2
    assert event_types(store, task.task_id).count("planner.decision") == 1


def test_unknown_result_is_handed_to_planner_and_not_retried() -> None:
    capability = UnknownCapability()
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[capability],
        planner=FakePlanner(
            [{"capability": "test.unknown", "arguments": {"value": "x"}}]
        ),
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "unknown"

    assert capability.calls == 1
    assert "planner.decision" in event_types(store, task.task_id)
    assert "task.retrying" not in event_types(store, task.task_id)


def test_unsupported_read_only_result_completes_with_warnings() -> None:
    planner = DecisionPlanner(
        PlannerDecision(
            action="unsupported",
            unsupported_intents=["web.research"],
            warnings=["unsupported intent: web.research"],
            reason="no registered web capability",
        )
    )
    container, store, _publisher, _registry = build_test_container(planner=planner)
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert stored.status == "succeeded"
    assert stored.result["warnings"] == ["unsupported intent: web.research"]
    assert "task.completed" in event_types(store, task.task_id)


def test_unsupported_write_requires_confirmation_before_partial_execution() -> None:
    planner = DecisionPlanner(
        PlannerDecision(
            action="unsupported",
            unsupported_intents=["form.submit"],
            requires_user_confirmation=True,
            reason="only a partial write can be performed",
        )
    )
    container, store, _publisher, _registry = build_test_container(planner=planner)
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "waiting_input"

    waiting = [
        item
        for item in store.list_events(task.task_id)
        if item.event_type == "task.waiting_input"
    ]
    assert waiting[-1].payload["missing_information"] == [
        "partial_execution_confirmation"
    ]


class LlmLikePlanner(FakePlanner):
    """A planner that reports itself as model backed, like the LLM planner."""

    name = "llm"
    last_call_count = 1


def test_deterministic_planner_is_not_charged_to_the_model_budget() -> None:
    """A pure-function planner must not consume ``max_model_calls``.

    The budget equals the maximum step count, so charging every
    ``decide_after_observation`` made an 8-step Plan impossible.
    """

    container, store, _publisher, _registry = build_test_container(
        planner=FakePlanner(
            [{"capability": "fake.read", "arguments": {"value": "x"}}]
        ),
        task_max_model_calls=1,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"
    assert store.get_task(task.task_id).model_call_count == 0


def test_model_backed_planner_budget_stops_planning_calls() -> None:
    steps = [
        {"capability": "fake.read", "arguments": {"value": str(index)}}
        for index in range(8)
    ]
    container, store, _publisher, _registry = build_test_container(
        planner=LlmLikePlanner(steps),
        task_max_model_calls=1,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "failed"
    assert store.get_task(task.task_id).last_error["message"] == "model call budget exceeded"


def test_openai_compatible_planner_parses_plan_and_decision() -> None:
    plan_json = """
    {
      "objective": "read a value",
      "steps": [
        {"capability": "fake.read", "arguments": {"value": "x"}}
      ]
    }
    """
    decision_json = """
    {
      "action": "complete",
      "required_input": [],
      "unsupported_intents": [],
      "warnings": [],
      "requires_user_confirmation": false,
      "reason": "done",
      "steps": []
    }
    """
    client = StubPlannerClient([plan_json, decision_json])
    planner = OpenAICompatiblePlanner(client)
    from app.ingress.chat import ChatIngress

    task = ChatIngress().create_task("user-1", {"text": "read"})
    constraints = PlanningConstraints()
    plan = planner.create_plan(
        task,
        [
            CapabilityDescriptor(
                name="fake.read",
                description="read",
                risk_level="read_only",
                side_effect=False,
                requires_approval=False,
                idempotent=True,
                timeout_seconds=5,
            )
        ],
        [],
        constraints,
        None,
    )
    decision = planner.decide_after_observation(
        task, plan, [], constraints, None
    )

    assert plan.steps[0].capability == "fake.read"
    assert decision.action == "complete"
    assert client.calls == 2

class PermanentFlakyCapability:
    """Fail every time, permanently: the capability cannot be made to work."""

    descriptor = CapabilityDescriptor(
        name="test.permanent",
        description="Always fail with a permanent error.",
        risk_level="read_only",
        side_effect=False,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=5,
    )

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": str(arguments.get("value") or "x")}

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise PermanentCapabilityError("cannot be done")


def test_a_replan_that_reissues_the_same_call_fails_instead_of_looping() -> None:
    """A model can replan a permanent failure into the identical call.

    Step ids are regenerated per Plan, so the completed-id guard cannot see
    it; without this check the Task burns its replan budget re-running a call
    that has already been proven to fail, and the external effect is doubled.
    """

    capability = PermanentFlakyCapability()
    replanner = FakeReplanner(
        initial_steps=[{"capability": "test.permanent", "arguments": {"value": "x"}}],
        replan_steps=[{"capability": "test.permanent", "arguments": {"value": "x"}}],
        trigger_classifications=("permanent_error",),
    )
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[capability],
        planner=replanner,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "failed"

    # The repeated call is refused, and refused before it is issued again.
    assert capability.calls == 1
    stored = store.get_task(task.task_id)
    assert stored.replan_count == 0
    assert "repeats an executed call" in (stored.last_error or {}).get("message", "")


def test_a_replan_with_different_arguments_is_still_allowed() -> None:
    """The guard keys on the call, not on the capability: a new call goes through.

    A retryable failure no longer reaches the planner at all (the in-place retry
    absorbs it), so the guard is exercised with a permanent failure whose
    replacement rewrites the arguments.
    """

    capability = PermanentFlakyCapability()
    replanner = FakeReplanner(
        initial_steps=[{"capability": "test.permanent", "arguments": {"value": "x"}}],
        replan_steps=[{"capability": "test.permanent", "arguments": {"value": "y"}}],
        trigger_classifications=("permanent_error",),
    )
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[capability],
        planner=replanner,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "failed"

    stored = store.get_task(task.task_id)
    assert stored.replan_count == 1
    assert capability.calls == 2


def test_an_unknown_result_cannot_be_replanned_into_a_second_call() -> None:
    """The effect may already have happened, so the write must not run again.

    The Planner may query the external state or give up, but a re-plan that
    would issue the same call a second time is refused before it is activated.
    """

    capability = UnknownCapability()
    replanner = FakeReplanner(
        initial_steps=[{"capability": "test.unknown", "arguments": {"value": "x"}}],
        replan_steps=[{"capability": "test.unknown", "arguments": {"value": "x"}}],
        trigger_classifications=("unknown_external_result",),
    )
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[capability],
        planner=replanner,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "unknown"

    assert capability.calls == 1, "the write must not be issued a second time"
    stored = store.get_task(task.task_id)
    assert stored.last_error["classification"] == "unknown_external_result"
    assert "plan.replanned" not in event_types(store, task.task_id)


def test_replan_without_a_plan_closes_with_the_planners_own_reason() -> None:
    """A model that says "replan" but names no steps is saying "there is no way".

    That is an answer, not a malformed one: the Task closes with the model's own
    words in its warnings instead of looking like a crash.
    """

    reason = "现有能力里没有能查 ICP 许可证的，做不了"
    planner = DecisionPlanner(PlannerDecision(action="replan", reason=reason))
    container, store, _publisher, _registry = build_test_container(planner=planner)
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert reason in (stored.result or {}).get("warnings", [])
    assert stored.last_error is None


def test_a_step_that_fails_validation_goes_back_to_the_planner() -> None:
    """A schema mismatch is the Planner's own output being wrong, so it repairs it.

    This used to be a hard stop: the Task failed without the Planner ever
    learning why. Here the first Step yields a value the second cannot accept,
    and the replacement the Planner produces works.
    """

    class YieldsTooShort:
        descriptor = CapabilityDescriptor(
            name="test.producer",
            description="Yields a value the next Step cannot accept.",
            risk_level="read_only",
            side_effect=False,
            requires_approval=False,
            idempotent=True,
            timeout_seconds=5,
        )

        def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
            return dict(arguments)

        def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
            # fake.read requires a non-empty value, so the resolved reference
            # fails the Step check the runtime runs before calling anything.
            return {"value": ""}

    replanner = FakeReplanner(
        initial_steps=[
            {"capability": "test.producer", "arguments": {}},
            {
                "capability": "fake.read",
                "arguments": {"value": "$steps.step-1.output.value"},
            },
        ],
        replan_steps=[{"capability": "fake.read", "arguments": {"value": "repaired"}}],
        trigger_classifications=("validation_error",),
    )
    container, store, _publisher, _registry = build_test_container(
        extra_capabilities=[YieldsTooShort()],
        planner=replanner,
    )
    task = create_task(container)

    assert container.execution_service.run_task(task.task_id) == "succeeded"

    stored = store.get_task(task.task_id)
    assert stored.replan_count == 1
    assert "plan.replanned" in event_types(store, task.task_id)
