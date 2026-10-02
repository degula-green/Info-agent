"""Calibrate the Laya fast-path gate on the internal validation split.

The frozen evaluation set must not drive threshold selection, so this script
re-builds the same stratified split the trainer used (same seed, same holdout
ratio) and sweeps `min_confidence` / `min_margin` on that split only. It reports
direct-path coverage and precision, the fallback ratio, and which intents are
still missed or wrongly accepted at the recommended gate.

Run from services/intent after training:

    ./.venv/Scripts/python.exe calibrate_intent_thresholds.py `
      --model-dir models/laya-intent-v6 `
      --dataset ../agent/tests/fixtures/laya_intent_dataset.jsonl `
      --json ../agent/tests/fixtures/laya_intent_v6_thresholds.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

import torch
from safetensors.torch import load_file
from transformers import AutoTokenizer

from laya.agent import _fix_tokenizer_config
from laya.common import build_model

HERE = Path(__file__).resolve().parent


def load_trainer():
    spec = importlib.util.spec_from_file_location(
        "train_intent_head", HERE / "train_intent_head.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score_items(trainer, model, items, pad_id, batch_size, device, use_amp):
    """Per-row confidence, margin and prediction for the validation split."""

    model.eval()
    scored: list[dict] = []
    with torch.no_grad():
        for start in range(0, len(items), batch_size):
            chunk = items[start : start + batch_size]
            batch = trainer.move_batch(trainer.collate(chunk, pad_id), device)
            with torch.autocast(
                device_type=device.type,
                dtype=torch.bfloat16,
                enabled=use_amp,
            ):
                logits, _ = model(
                    batch["input_ids"],
                    batch["attention_mask"],
                    batch["marker_pos"],
                    batch["marker_mask"],
                    batch["qtype"],
                    detach_encoder=False,
                )
            mask = batch["marker_mask"]
            probabilities = torch.softmax(logits.masked_fill(~mask, -1e4), -1)
            for index, item in enumerate(chunk):
                values = probabilities[index, : len(item["markers"])].tolist()
                ranked = sorted(values, reverse=True)
                scored.append(
                    {
                        "label": int(item["label"]),
                        "prediction": int(max(range(len(values)), key=values.__getitem__)),
                        "confidence": ranked[0],
                        "margin": ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0],
                    }
                )
    return scored


def sweep(scored: list[dict], target_precision: float) -> tuple[dict | None, list[dict]]:
    best: dict | None = None
    table: list[dict] = []
    for step in range(50):
        confidence = round(0.50 + 0.01 * step, 2)
        for margin_step in range(1, 11):
            margin = round(0.05 * margin_step, 2)
            accepted = [
                row
                for row in scored
                if row["confidence"] >= confidence and row["margin"] >= margin
            ]
            if not accepted:
                continue
            correct = sum(1 for row in accepted if row["prediction"] == row["label"])
            entry = {
                "min_confidence": confidence,
                "min_margin": margin,
                "coverage": round(len(accepted) / len(scored), 4),
                "precision": round(correct / len(accepted), 4),
                "fallback_ratio": round(1 - len(accepted) / len(scored), 4),
            }
            table.append(entry)
            if entry["precision"] >= target_precision and (
                best is None or entry["coverage"] > best["coverage"]
            ):
                best = entry
    return best, table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--target-precision", type=float, default=0.95)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-len", type=int, default=384)
    parser.add_argument("--head-max-len", type=int, default=256)
    parser.add_argument("--holdout-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--full-table", action="store_true")
    args = parser.parse_args()

    trainer = load_trainer()
    torch.set_num_threads(max(1, min(os.cpu_count() or 1, 24)))
    device = torch.device(
        ("cuda" if torch.cuda.is_available() else "cpu")
        if args.device == "auto"
        else args.device
    )
    use_amp = device.type == "cuda"

    model_dir = args.model_dir
    _fix_tokenizer_config(str(model_dir))
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir / "tokenizer"))
    cfg = json.loads((model_dir / "rl_agent_config.json").read_text(encoding="utf-8"))
    cfg["max_len"] = args.max_len
    cfg["head_max_len"] = args.head_max_len
    cfg["gradient_checkpointing"] = False

    rows = trainer.load_rows(args.dataset)
    _train_rows, validation_rows = trainer.stratified_split(
        rows, args.holdout_ratio, args.seed
    )
    items = trainer.build_items(validation_rows, tokenizer, cfg)

    model = build_model(cfg, encoder_dir=str(model_dir / "encoder"))
    model.load_state_dict(load_file(str(model_dir / "model.safetensors")), strict=True)
    model.to(device)

    scored = score_items(
        trainer, model, items, tokenizer.pad_token_id, args.batch_size, device, use_amp
    )
    best, table = sweep(scored, args.target_precision)

    misses = Counter()
    false_accepts = Counter()
    if best is not None:
        for row in scored:
            if row["confidence"] < best["min_confidence"] or row["margin"] < best["min_margin"]:
                continue
            if row["prediction"] != row["label"]:
                misses[trainer.INTENTS[row["label"]]] += 1
                false_accepts[trainer.INTENTS[row["prediction"]]] += 1

    report = {
        "schema_version": trainer.SCHEMA_VERSION,
        "option_order": list(trainer.INTENTS),
        "model_dir": str(model_dir),
        "dataset": str(args.dataset),
        "validation_rows": len(scored),
        "split": {
            "holdout_ratio": args.holdout_ratio,
            "seed": args.seed,
            "dataset_rows": len(rows),
        },
        "target_precision": args.target_precision,
        "recommended": best,
        "accepted_missed_by_actual": dict(misses),
        "accepted_false_positive_by_prediction": dict(false_accepts),
    }
    if args.full_table:
        report["sweep"] = table
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
