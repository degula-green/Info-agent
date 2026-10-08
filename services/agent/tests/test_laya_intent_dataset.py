"""Integrity checks for the Laya intent datasets.

The fine-tuning script fails on bad labels and leakage at training time; these
offline checks make the same guarantees visible in the normal test run, and pin
the coverage the migration document requires.

The JSONL fixtures below are frozen artifacts built for the intent-v6 label
set. ``person.query`` and ``report.weekly`` were added in intent-v7 and are
recognised by the remote Jev provider, whose criteria are sent per request.
Extending the Laya datasets for the new labels is deferred with the Laya
pipeline itself (the pinned checkpoint already fails the v7 contract check via
``LayaUnderstandingProvider``), so coverage is asserted over the labels the
fixtures actually carry.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.understanding.schema import ALL_INTENT_LABELS, INTENT_OPTION_ORDER

FIXTURES = Path(__file__).parent / "fixtures"
TRAIN_PATH = FIXTURES / "laya_intent_dataset.jsonl"
EVAL_V6_PATH = FIXTURES / "laya_intent_eval_v6.jsonl"
HANDWRITTEN_PATH = FIXTURES / "laya_intent_eval_v6_handwritten.jsonl"
MIGRATED_PATHS = (
    FIXTURES / "laya_intent_eval.jsonl",
    FIXTURES / "laya_intent_eval2.jsonl",
)

# Labels that exist in the live contract but not yet in the frozen Laya
# datasets. Keep this list shrink-only: it must be empty once the datasets are
# regenerated.
DEFERRED_LABELS = frozenset({"person.query", "report.weekly"})
FROZEN_LABELS = set(INTENT_OPTION_ORDER) - DEFERRED_LABELS


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if line.strip():
            row = json.loads(line)
            assert row["intent"] in ALL_INTENT_LABELS, f"{path.name}:{line_number}"
            rows.append(row)
    return rows


def normalize(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def test_training_set_meets_coverage_target() -> None:
    rows = load(TRAIN_PATH)
    counts = Counter(row["intent"] for row in rows)
    assert set(counts) == FROZEN_LABELS
    assert len(rows) >= 700
    for name in FROZEN_LABELS:
        assert counts[name] >= 100, f"{name}: {counts[name]}"


def test_frozen_evaluation_set_meets_coverage_target() -> None:
    rows = load(EVAL_V6_PATH)
    counts = Counter(row["intent"] for row in rows)
    assert set(counts) == FROZEN_LABELS
    for name in FROZEN_LABELS:
        assert counts[name] >= 30, f"{name}: {counts[name]}"
    handwritten = {" ".join(row["text"].split()) for row in load(HANDWRITTEN_PATH)}
    assert handwritten, "the hand-written evaluation split is empty"
    rows_text = {" ".join(row["text"].split()) for row in rows}
    overlap = {text for text in handwritten if text in rows_text}
    assert len(overlap) / len(rows) >= 0.5, "hand-written share below 50%"


def test_training_and_frozen_evaluation_sets_do_not_leak() -> None:
    train = {normalize(row["text"]) for row in load(TRAIN_PATH)}
    evaluation = {normalize(row["text"]) for row in load(EVAL_V6_PATH)}
    assert train.isdisjoint(evaluation)


def test_no_dataset_repeats_a_text_within_itself() -> None:
    for path in (TRAIN_PATH, EVAL_V6_PATH, HANDWRITTEN_PATH, *MIGRATED_PATHS):
        rows = load(path)
        texts = [normalize(row["text"]) for row in rows]
        assert len(texts) == len(set(texts)), f"{path.name} contains duplicate text"


def test_migrated_fixtures_are_free_of_legacy_labels() -> None:
    retired = {"form.prepare", "form.submit", "document.compare"}
    for path in MIGRATED_PATHS:
        labels = {row["intent"] for row in load(path)}
        assert not (labels & retired), path.name
