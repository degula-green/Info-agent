"""Evaluate laya-intent-v6 on the frozen evaluation set and calibrate thresholds.

Unlike ``tests/test_understanding_corpus.py`` this measures the real model, so
it needs the Laya sidecar running with the v6 checkpoint and is not part of the
offline test run.

Run from services/agent:

    ./.venv/Scripts/python.exe scripts/eval_laya_intents.py --json report.json
    ./.venv/Scripts/python.exe scripts/eval_laya_intents.py --gate

The report contains top-1, macro-F1, per-intent recall, the confusion matrix
and the direct-path threshold sweep (coverage, precision and fallback ratio).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from app.config import Settings  # noqa: E402
from app.infrastructure.laya.client import HttpLayaClient  # noqa: E402
from app.understanding.laya import score_text  # noqa: E402
from app.understanding.schema import (  # noqa: E402
    BOUNDARY_INTENTS,
    INTENT_NAMES,
    INTENT_OPTION_ORDER,
    INTENT_SCHEMA_VERSION,
)

DEFAULT_DATASET = ROOT / "tests" / "fixtures" / "laya_intent_eval_v6.jsonl"


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if row["intent"] not in INTENT_OPTION_ORDER:
            raise ValueError(f"{path}:{line_number}: unknown intent {row['intent']!r}")
        rows.append(row)
    return rows


def margin_of(probabilities: dict[str, float]) -> float:
    ordered = sorted(probabilities.values(), reverse=True)
    if len(ordered) < 2:
        return ordered[0] if ordered else 0.0
    return ordered[0] - ordered[1]


def metrics(rows: list[dict], predictions: list[str]) -> dict:
    confusion = {
        actual: Counter() for actual in INTENT_OPTION_ORDER
    }
    for row, predicted in zip(rows, predictions):
        confusion[row["intent"]][predicted] += 1

    per_intent: dict[str, dict[str, float]] = {}
    f1_scores: list[float] = []
    for name in INTENT_OPTION_ORDER:
        support = sum(confusion[name].values())
        predicted_total = sum(confusion[actual][name] for actual in INTENT_OPTION_ORDER)
        correct = confusion[name][name]
        recall = correct / support if support else 0.0
        precision = correct / predicted_total if predicted_total else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )
        f1_scores.append(f1)
        per_intent[name] = {
            "support": support,
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "f1": round(f1, 4),
        }
    correct = sum(confusion[name][name] for name in INTENT_OPTION_ORDER)
    return {
        "top1": round(correct / max(1, len(rows)), 4),
        "macro_f1": round(sum(f1_scores) / len(INTENT_OPTION_ORDER), 4),
        "per_intent": per_intent,
        "confusion_matrix": {
            actual: dict(counts)
            for actual, counts in confusion.items()
            if counts
        },
    }


def calibrate(rows: list[dict], scores: list[dict], min_precision: float) -> dict:
    """Sweep confidence/margin and keep the widest gate that stays precise."""

    best: dict | None = None
    sweep: list[dict] = []
    for confidence in [round(0.50 + 0.01 * step, 2) for step in range(50)]:
        for margin in [round(0.05 * step, 2) for step in range(1, 11)]:
            accepted = [
                (row, score)
                for row, score in zip(rows, scores)
                if score["confidence"] >= confidence and score["margin"] >= margin
            ]
            if not accepted:
                continue
            correct = sum(
                1 for row, score in accepted if score["label"] == row["intent"]
            )
            precision = correct / len(accepted)
            coverage = len(accepted) / len(rows)
            sweep.append(
                {
                    "min_confidence": confidence,
                    "min_margin": margin,
                    "coverage": round(coverage, 4),
                    "precision": round(precision, 4),
                }
            )
            if precision >= min_precision and (
                best is None or coverage > best["coverage"]
            ):
                best = {
                    "min_confidence": confidence,
                    "min_margin": margin,
                    "coverage": round(coverage, 4),
                    "precision": round(precision, 4),
                }
    return {
        "target_precision": min_precision,
        "recommended": best,
        "sweep": sweep,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--min-precision", type=float, default=0.95)
    parser.add_argument(
        "--gate",
        action="store_true",
        help="exit non-zero unless every published threshold in the plan passes",
    )
    args = parser.parse_args()

    settings = Settings()
    if not settings.laya_base_url:
        print("AGENT_LAYAYA_BASE_URL is required")
        return 2
    client = HttpLayaClient(
        base_url=settings.laya_base_url,
        api_key=settings.laya_api_key,
        model=settings.laya_model,
        timeout_seconds=max(5.0, settings.laya_timeout_seconds),
    )

    rows = load_rows(args.dataset)
    started = time.time()
    predictions: list[str] = []
    scores: list[dict] = []
    for row in rows:
        label, confidence, probabilities = score_text(client, str(row["text"]))
        predictions.append(label)
        scores.append(
            {
                "label": label,
                "confidence": confidence,
                "margin": margin_of(probabilities),
                "probabilities": probabilities,
            }
        )

    result = metrics(rows, predictions)
    result.update(
        {
            "schema_version": INTENT_SCHEMA_VERSION,
            "option_order": list(INTENT_OPTION_ORDER),
            "dataset": str(args.dataset),
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "rows": len(rows),
            "elapsed_seconds": round(time.time() - started, 1),
            "calibration": calibrate(rows, scores, args.min_precision),
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json is not None:
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.gate:
        failures: list[str] = []
        if result["top1"] < 0.95:
            failures.append(f"top1 {result['top1']} < 0.95")
        if result["macro_f1"] < 0.95:
            failures.append(f"macro_f1 {result['macro_f1']} < 0.95")
        for name in sorted(INTENT_NAMES):
            floor = 0.95 if name == "compliance.assess" else 0.90
            recall = result["per_intent"][name]["recall"]
            if recall < floor:
                failures.append(f"{name} recall {recall} < {floor}")
        for name in sorted(BOUNDARY_INTENTS):
            recall = result["per_intent"][name]["recall"]
            if recall < 0.90:
                failures.append(f"{name} recall {recall} < 0.90")
        if failures:
            print(json.dumps({"gate": "failed", "failures": failures}, ensure_ascii=False))
            return 1
        print(json.dumps({"gate": "passed"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
