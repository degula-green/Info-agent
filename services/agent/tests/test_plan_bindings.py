"""Planner-facing references: strict schema in, Runtime references out.

The Planner cannot carry a fetched page inside its own answer, so web.extract
names the step that has the bytes and answer.compose names the step that has
the evidence. Since web.research became the Planner-visible reader, the
evidence a composer may cite comes from that step; web.extract's document_ref
stays covered because the research pipeline still composes it internally.
These tests pin both halves of that contract: the JSON Schema the model is
forced to fill in, and the binder that turns the model's answer into the
``$steps.<step_id>.output.<field>`` form the Runtime already resolves.
"""

from __future__ import annotations

import pytest

from app.capabilities.answer import AnswerComposeCapability
from app.capabilities.web import WebExtractCapability, WebFetchCapability
from app.capabilities.web_research import WebResearchCapability
from app.kernel.bindings import bind_plan_references
from app.kernel.errors import ContractValidationError
from app.kernel.models import CapabilityDescriptor, Plan, PlanStep
from app.planning.schema import planner_arguments_schema
from app.testing.fake_providers import FakeAnswerProvider, FakePageFetcher

URL = "https://93.184.216.34/page"


class _UnusedReader:
    """Registered only for its descriptor; no test here reads a page."""

    def read(self, url: str):  # pragma: no cover - never reached
        raise AssertionError(f"reader must not be called: {url}")


def descriptors() -> list[CapabilityDescriptor]:
    return [
        WebFetchCapability(FakePageFetcher()).descriptor,
        WebExtractCapability().descriptor,
        WebResearchCapability(_UnusedReader(), search_provider=None, aliases={}).descriptor,
        AnswerComposeCapability(FakeAnswerProvider()).descriptor,
    ]


def plan_with(steps: list[PlanStep]) -> Plan:
    return Plan(plan_id="p1", task_id="t1", objective="读网页", steps=steps)


def step(order: int, capability: str, arguments: dict) -> PlanStep:
    return PlanStep(
        step_id=f"p1-step-{order}",
        plan_id="p1",
        order=order,
        capability=capability,
        arguments=arguments,
    )


def test_the_planner_schema_asks_for_a_reference_instead_of_raw_bytes() -> None:
    schema = planner_arguments_schema(WebExtractCapability().descriptor)

    assert schema["additionalProperties"] is False
    assert "document" not in schema["properties"]
    assert "document_ref" in schema["properties"]
    assert "document_ref" in schema["required"]
    reference = schema["properties"]["document_ref"]
    assert set(reference["required"]) == {"step", "output"}
    assert reference["additionalProperties"] is False


def test_the_answer_schema_asks_for_the_evidence_reference() -> None:
    schema = planner_arguments_schema(AnswerComposeCapability(FakeAnswerProvider()).descriptor)

    assert "evidence_ref" in schema["properties"]
    assert "evidence_ref" in schema["required"]
    assert "evidence" not in schema["properties"]


def test_the_task_text_argument_is_hidden_from_the_model() -> None:
    """A value the model may paraphrase cannot be a trust anchor.

    web.research decides which URLs it may fetch by looking for them in the
    request text, so the request comes from the Task, not from the model.
    """

    descriptor = WebResearchCapability(
        _UnusedReader(), search_provider=None, aliases={}
    ).descriptor

    schema = planner_arguments_schema(descriptor)

    assert "request" not in schema["properties"]
    assert "request" not in schema["required"]
    # Everything else still reaches the model.
    assert "urls" in schema["properties"]
    assert "queries" in schema["properties"]


def test_the_binder_turns_planner_references_into_runtime_references() -> None:
    plan = plan_with(
        [
            step(1, "web.fetch", {"url": URL}),
            step(2, "web.extract", {"document_ref": {"step": 1, "output": "content"}}),
            step(
                3,
                "web.research",
                {"request": "读一下这个网页", "urls": [URL]},
            ),
            step(
                4,
                "answer.compose",
                {
                    "question": "这个页面讲了什么",
                    "evidence_ref": {"step": 3, "output": "evidence"},
                },
            ),
        ]
    )

    bound = bind_plan_references(plan, descriptors())

    assert bound.steps[1].arguments == {
        "document": "$steps.p1-step-1.output.content"
    }
    assert bound.steps[3].arguments == {
        "question": "这个页面讲了什么",
        "evidence": "$steps.p1-step-3.output.evidence",
    }
    # The Planner's own plan is left as it was; binding is not an in-place edit.
    assert plan.steps[1].arguments["document_ref"] == {"step": 1, "output": "content"}


def test_a_plan_without_reference_arguments_is_unchanged() -> None:
    plan = plan_with([step(1, "web.fetch", {"url": URL})])

    bound = bind_plan_references(plan, descriptors())

    assert bound.steps[0].arguments == {"url": URL}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"document_ref": {"step": 2, "output": "content"}}, "earlier"),
        ({"document_ref": {"step": 9, "output": "content"}}, "missing step"),
        ({"document_ref": {"step": 1, "output": "text"}}, "expected content"),
        ({"document_ref": {"step": 1}}, "not a valid step reference"),
    ],
)
def test_a_reference_that_could_never_resolve_is_refused(
    arguments: dict, message: str
) -> None:
    plan = plan_with(
        [
            step(1, "web.fetch", {"url": URL}),
            step(2, "web.extract", arguments),
        ]
    )

    with pytest.raises(ContractValidationError) as excinfo:
        bind_plan_references(plan, descriptors())

    assert message in str(excinfo.value)


def test_a_reference_to_the_wrong_capability_is_refused() -> None:
    plan = plan_with(
        [
            step(1, "web.fetch", {"url": URL}),
            step(
                2,
                "answer.compose",
                {
                    "question": "这个页面讲了什么",
                    "evidence_ref": {"step": 1, "output": "evidence"},
                },
            ),
        ]
    )

    with pytest.raises(ContractValidationError) as excinfo:
        bind_plan_references(plan, descriptors())

    assert "expected web.research" in str(excinfo.value)


def test_a_step_that_carries_both_forms_is_refused() -> None:
    """The model wrote the reference into the runtime field as well."""

    plan = plan_with(
        [
            step(1, "web.fetch", {"url": URL}),
            step(
                2,
                "web.extract",
                {
                    "document_ref": {"step": 1, "output": "content"},
                    "document": "$steps.p1-step-1.output.content",
                },
            ),
        ]
    )

    with pytest.raises(ContractValidationError) as excinfo:
        bind_plan_references(plan, descriptors())

    assert "contains both document_ref and document" in str(excinfo.value)


def test_a_step_without_the_required_reference_stays_a_plain_schema_error() -> None:
    """The binder does not invent a reference the model never wrote."""

    plan = plan_with([step(1, "web.extract", {"url": URL})])

    bound = bind_plan_references(plan, descriptors())

    assert bound.steps[0].arguments == {"url": URL}
