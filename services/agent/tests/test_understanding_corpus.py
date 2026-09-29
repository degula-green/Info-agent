"""Corpus-driven checks for the ingress gate and the intent contract.

These run offline: the gate, the schema and the planner are deterministic, so
the whole corpus can be replayed on every commit. Recognition accuracy of the
*model* is measured separately by scripts/eval_understanding.py, which needs a
live LLM and is therefore not part of the test run.

The product decision behind this file: a schedule and a to-do are the same
object. There is one intent (``todo.create``) covering meetings, invitations,
reminders and plain work, and time is optional in every one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingress.chat import ChatIngress
from app.ingress.knowledge_events import KnowledgeEventIngress
from app.kernel.models import CapabilityDescriptor, TaskUnderstanding, UnderstandingIntent
from app.planning.deterministic import DEFAULT_CAPABILITY_NAME, DeterministicPlanner
from app.understanding.schema import INTENT_NAMES

CORPUS_PATH = Path(__file__).parent / "fixtures" / "understanding_corpus.json"


def load_corpus() -> list[dict]:
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert data["version"] == 2
    return data["cases"]


CORPUS = load_corpus()
COLLECTED = [case for case in CORPUS if case["source"] == "collected"]
CHAT = [case for case in CORPUS if case["source"] == "chat"]


def ids(cases: list[dict]) -> list[str]:
    return [case["id"] for case in cases]


def test_corpus_covers_every_required_category() -> None:
    """The step-2.5 doc lists what the fixed corpus must cover."""

    tags = {tag for case in CORPUS for tag in case["tags"]}
    required = {
        "chitchat",
        "false_positive",
        "todo",
        "meeting_as_todo",
        "no_time",
        "coarse_time",
        "mixed",
        "out_of_vocab",
        "prompt_injection",
    }
    assert required <= tags, sorted(required - tags)
    # Every intent in the catalog must appear in the corpus at least once, so a
    # catalog change cannot silently go untested.
    expected = {name for case in CORPUS for name in case["expect"]}
    assert expected == set(INTENT_NAMES), sorted(set(INTENT_NAMES) - expected)


def test_every_expected_and_forbidden_intent_exists_in_the_catalog() -> None:
    for case in CORPUS:
        for name in case["expect"] + case["forbid"]:
            assert name in INTENT_NAMES, f"{case['id']}: unknown intent {name}"


def test_no_case_still_references_the_retired_calendar_intent() -> None:
    """The merge is the whole point of this corpus revision.

    A leftover ``calendar.create`` would either be an unknown intent (a schema
    error at runtime) or, worse, a case asserting the split still exists.
    """

    for case in CORPUS:
        for name in case["expect"] + case["forbid"]:
            assert name != "calendar.create", case["id"]


@pytest.mark.parametrize("case", COLLECTED, ids=ids(COLLECTED))
def test_collected_gate_decision_matches_the_corpus(case: dict) -> None:
    """The ingress gate must drop noise and let every candidate through."""

    ingress = KnowledgeEventIngress()
    allowed = ingress.task_candidate_hint(case["text"])

    assert allowed is (case["gate"] == "pass"), (
        f"{case['id']}: gate={case['gate']} but allowed={allowed}"
    )


@pytest.mark.parametrize("case", CORPUS, ids=ids(CORPUS))
def test_gate_never_drops_a_case_that_must_be_recognised(case: dict) -> None:
    """A case that expects an intent or is_task must survive the gate.

    This is the regression that matters: "完成登录模块代码" carries no time and
    no schedule keyword, so a schedule-only gate dropped it before the
    understanding layer ever saw it.
    """

    if case["source"] != "collected":
        pytest.skip("the chat ingress has no pre-filter")
    if not (case["is_task"] or case["expect"]):
        pytest.skip("noise case, covered by the gate test")

    assert KnowledgeEventIngress().task_candidate_hint(case["text"]) is True, case["id"]


def planner(min_confidence: float = 0.7) -> DeterministicPlanner:
    return DeterministicPlanner(default_timezone="Asia/Shanghai", min_confidence=min_confidence)


def todo_descriptor() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=DEFAULT_CAPABILITY_NAME,
        description="创建待办",
        risk_level="external_write",
        side_effect=True,
        requires_approval=True,
        idempotent=True,
        timeout_seconds=10,
    )


def understand_from(case: dict, confidence: float = 0.95) -> TaskUnderstanding:
    # is_task is None for adversarial inputs: those only assert that no
    # injected intent leaked through, so the frame is built as a task.
    is_task = bool(case["is_task"]) if case["is_task"] is not None else True
    return TaskUnderstanding(
        is_task=is_task,
        goal=case["text"],
        task_kind="action" if is_task else None,
        intent_candidates=[
            UnderstandingIntent(name=name, confidence=confidence)
            for name in case["expect"]
        ],
        confidence=confidence,
        reason=f"corpus {case['id']}",
    )


@pytest.mark.parametrize("case", CORPUS, ids=ids(CORPUS))
def test_planner_drafts_a_todo_exactly_when_the_corpus_expects_one(case: dict) -> None:
    """With correct understanding, the plan matches what the corpus expects.

    todo.create is the only capability registered today, so it is the only
    intent that can become a Step. Every other intent must be reported as
    unsupported instead of being invented.
    """

    if case["is_task"] is None:
        pytest.skip("adversarial case: is_task is not asserted")

    task = ChatIngress().create_task("user-1", {"text": case["text"]})
    plan = planner().create_plan(
        task, [todo_descriptor()], [], None, understand_from(case)
    )

    wants_todo = case["is_task"] and DEFAULT_CAPABILITY_NAME in case["expect"]
    assert bool(plan.steps) is wants_todo, case["id"]
    if case["is_task"] and not wants_todo:
        assert set(plan.unsupported_intents) == set(case["expect"]), case["id"]


@pytest.mark.parametrize("case", CORPUS, ids=ids(CORPUS))
def test_missing_todo_capability_yields_no_steps(case: dict) -> None:
    """A collected message must not become a draft if the capability is gone."""

    if DEFAULT_CAPABILITY_NAME not in case["expect"]:
        pytest.skip("not a todo case")

    task = ChatIngress().create_task("user-1", {"text": case["text"]})
    plan = planner().create_plan(task, [], [], None, understand_from(case))

    assert plan.steps == []
    assert DEFAULT_CAPABILITY_NAME in plan.unsupported_intents
