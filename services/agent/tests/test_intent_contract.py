"""Contract tests for the intent-v6 migration.

The taxonomy lives in ``app/understanding/intent_contract.json``. These tests
pin the frozen option order, prove that the Agent, the Laya provider and the
fine-tuning tooling read the same definitions, and prove that legacy labels
cannot slip back in through any of those surfaces.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.understanding import laya as laya_module
from app.infrastructure.laya.client import LayaError
from app.understanding.laya import build_question, verify_model_contract
from app.understanding.prompt import SYSTEM_PROMPT
from app.understanding.schema import (
    ALL_INTENT_LABELS,
    BOUNDARY_INTENTS,
    CONTRACT_PATH,
    INTENT_CATALOG,
    INTENT_NAMES,
    INTENT_OPTION_ORDER,
    INTENT_SCHEMA_VERSION,
    LAYAYA_CRITERIA,
    LAYAYA_INSTRUCTION,
    TaskUnderstandingDraft,
)

EXPECTED_ORDER = (
    "todo.create",
    "knowledge.answer",
    "web.research",
    "compliance.assess",
    "form.complete",
    "non_task",
    "other_task",
)

LEGACY_LABELS = ("document.compare", "form.prepare", "form.submit")

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_option_order_is_frozen() -> None:
    assert INTENT_SCHEMA_VERSION == "intent-v6"
    assert INTENT_OPTION_ORDER == EXPECTED_ORDER
    assert ALL_INTENT_LABELS == set(EXPECTED_ORDER)


def test_contract_file_matches_loaded_order() -> None:
    raw = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert tuple(option["name"] for option in raw["options"]) == EXPECTED_ORDER
    assert raw["schema_version"] == INTENT_SCHEMA_VERSION
    assert raw["instruction"] == LAYAYA_INSTRUCTION


def test_business_and_boundary_split() -> None:
    assert INTENT_NAMES == {
        "todo.create",
        "knowledge.answer",
        "web.research",
        "compliance.assess",
        "form.complete",
    }
    assert BOUNDARY_INTENTS == {"non_task", "other_task"}
    assert {item.name for item in INTENT_CATALOG} == INTENT_NAMES


def test_form_complete_is_one_intent_and_excludes_submission() -> None:
    assert "form.complete" in INTENT_NAMES
    for split_label in ("form.prepare", "form.submit", "form.preview", "form.confirm"):
        assert split_label not in ALL_INTENT_LABELS
    definition = next(item for item in INTENT_CATALOG if item.name == "form.complete")
    assert definition.task_kind == "action"
    assert "不向外部平台提交" in definition.description
    criteria = definition.laya_criteria.casefold()
    assert "submit" in criteria and "not this intent" in criteria


def test_laya_criteria_follow_the_contract_order() -> None:
    assert laya_module._ordered_criteria() == {
        name: LAYAYA_CRITERIA[name] for name in EXPECTED_ORDER
    }
    criteria = build_question()["intent"]["criteria"]
    assert tuple(criteria) == EXPECTED_ORDER
    assert laya_module.LayaUnderstandingProvider.schema_version == "intent-v6"


def test_model_contract_check_accepts_a_matching_checkpoint(tmp_path: Path) -> None:
    (tmp_path / "rl_agent_config.json").write_text(
        json.dumps(
            {
                "intent_schema_version": "intent-v6",
                "option_order": list(EXPECTED_ORDER),
            }
        ),
        encoding="utf-8",
    )

    config = verify_model_contract(tmp_path)

    assert config["option_order"] == list(EXPECTED_ORDER)


@pytest.mark.parametrize(
    "config",
    [
        {"option_order": list(EXPECTED_ORDER)},
        {"intent_schema_version": "intent-v5", "option_order": list(EXPECTED_ORDER)},
        {"intent_schema_version": "intent-v6", "option_order": ["todo.create"]},
    ],
)
def test_model_contract_check_rejects_a_mismatched_checkpoint(
    tmp_path: Path, config: dict
) -> None:
    (tmp_path / "rl_agent_config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )

    with pytest.raises(LayaError):
        verify_model_contract(tmp_path)


def test_model_contract_check_rejects_a_missing_config(tmp_path: Path) -> None:
    with pytest.raises(LayaError):
        verify_model_contract(tmp_path)


def test_understanding_prompt_states_the_form_boundary() -> None:
    assert "form.complete" in SYSTEM_PROMPT
    assert "没有对应的执行能力" in SYSTEM_PROMPT
    assert "other_task" in SYSTEM_PROMPT


@pytest.mark.parametrize("legacy", LEGACY_LABELS)
def test_schema_rejects_legacy_intent_candidates(legacy: str) -> None:
    with pytest.raises(ValueError):
        TaskUnderstandingDraft.model_validate(
            {
                "is_task": True,
                "goal": "帮我提交表单",
                "task_kind": "action",
                "intent_candidates": [{"name": legacy, "confidence": 0.9}],
                "confidence": 0.9,
                "reason": "legacy",
            }
        )


def test_schema_rejects_unknown_intent_candidates() -> None:
    with pytest.raises(ValueError):
        TaskUnderstandingDraft.model_validate(
            {
                "is_task": True,
                "goal": "做点别的",
                "task_kind": "action",
                "intent_candidates": [{"name": "book.ticket", "confidence": 0.9}],
                "confidence": 0.9,
                "reason": "unknown",
            }
        )


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_training_tooling_reads_the_same_contract() -> None:
    module = _load_module(
        "intent_contract_training",
        REPO_ROOT / "services" / "intent" / "intent_contract.py",
    )
    contract = module.load_contract()
    assert module.option_order(contract) == EXPECTED_ORDER
    assert module.criteria(contract) == LAYAYA_CRITERIA
    assert str(contract["schema_version"]) == INTENT_SCHEMA_VERSION


def test_dataset_generator_reads_the_same_contract() -> None:
    module = _load_module(
        "build_laya_intent_dataset",
        REPO_ROOT / "services" / "agent" / "scripts" / "build_laya_intent_dataset.py",
    )
    assert tuple(module.INTENT_PROMPTS) == EXPECTED_ORDER


def test_dataset_generator_migrates_legacy_labels_explicitly() -> None:
    module = _load_module(
        "build_laya_intent_dataset_migration",
        REPO_ROOT / "services" / "agent" / "scripts" / "build_laya_intent_dataset.py",
    )
    assert module.LEGACY_INTENT_MIGRATION == {
        "form.prepare": "form.complete",
        "document.compare": "other_task",
    }
    assert module.migrate_case(
        {"id": "a", "text": "填表", "expect": ["form.prepare"], "is_task": True}
    ) == (True, ["form.complete"])
    assert module.migrate_case(
        {"id": "b", "text": "对比材料", "expect": ["document.compare"], "is_task": True}
    ) == (True, [])
    assert module.migrate_case(
        {"id": "c", "text": "晚上好", "expect": [], "is_task": False}
    ) == (False, [])
    with pytest.raises(module.LegacyLabelError):
        module.migrate_case(
            {"id": "d", "text": "提交表单", "expect": ["form.submit"], "is_task": True}
        )
