"""Fine-tune the Laya decision head for the Agent's intent-v6 labels.

Labels, order and criteria come from the shared intent contract. Unknown labels
fail the run instead of being skipped, legacy labels are rejected outright, and
text duplicated across the training pool or the frozen evaluation set aborts
training so a leaked sample cannot inflate the metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from collections import defaultdict
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

from intent_contract import criteria, load_contract, option_order
from laya.agent import _fix_tokenizer_config
from laya.common import QTYPES, build_model, build_sequence

MODEL_REPO = "convaiinnovations/laya-multilingual"
MODEL_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"

CONTRACT = load_contract()
SCHEMA_VERSION = str(CONTRACT["schema_version"])
INSTRUCTION = str(CONTRACT["instruction"])
INTENTS = option_order(CONTRACT)
CRITERIA = criteria(CONTRACT)

LEGACY_LABELS = frozenset({"document.compare", "form.prepare", "form.submit"})


class DatasetError(ValueError):
    """Raised when an input row cannot be trusted as an intent-v6 label."""


def normalize_text(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def load_rows(path: Path) -> list[dict]:
    """Load one JSONL dataset, rejecting legacy and unknown labels."""

    rows: list[dict] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        intent = str(row.get("intent", ""))
        if intent in LEGACY_LABELS:
            raise DatasetError(
                f"{path}:{line_number}: legacy label {intent!r} is not valid under "
                f"{SCHEMA_VERSION}; relabel the row first"
            )
        if intent not in INTENTS:
            raise DatasetError(
                f"{path}:{line_number}: unknown label {intent!r}; "
                f"{SCHEMA_VERSION} allows: {', '.join(INTENTS)}"
            )
        text = str(row.get("text") or "").strip()
        if not text:
            raise DatasetError(f"{path}:{line_number}: empty text")
        rows.append({"text": text, "intent": intent, "source": row.get("source")})
    return rows


def assert_no_duplicates(*groups: tuple[str, list[dict]]) -> None:
    """Abort when the same normalized text appears in two datasets or twice."""

    seen: dict[str, str] = {}
    for name, rows in groups:
        for row in rows:
            key = normalize_text(row["text"])
            previous = seen.get(key)
            if previous is not None:
                raise DatasetError(
                    f"duplicate text between {previous} and {name}: {row['text']!r}"
                )
            seen[key] = name


def stratified_split(
    rows: list[dict],
    holdout_ratio: float,
    seed: int,
) -> tuple[list[dict], list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["intent"])].append(row)
    train_rows: list[dict] = []
    validation_rows: list[dict] = []
    rng = random.Random(seed)
    for intent_rows in grouped.values():
        rng.shuffle(intent_rows)
        holdout = max(1, round(len(intent_rows) * holdout_ratio))
        holdout = min(holdout, max(1, len(intent_rows) - 1))
        validation_rows.extend(intent_rows[:holdout])
        train_rows.extend(intent_rows[holdout:])
    rng.shuffle(train_rows)
    rng.shuffle(validation_rows)
    return train_rows, validation_rows


def build_items(rows: list[dict], tokenizer, cfg: dict) -> list[dict]:
    question = {"t": "choice", "ins": INSTRUCTION, "crit": CRITERIA}
    items: list[dict] = []
    for row in rows:
        intent = str(row["intent"])
        sequence, markers = build_sequence(
            tokenizer,
            {"text": str(row["text"])},
            question,
            cfg["max_len"],
            cfg["head_max_len"],
        )
        if len(markers) != len(INTENTS):
            raise RuntimeError(
                f"Laya built {len(markers)} option markers for {len(INTENTS)} intents"
            )
        target = [0.0] * len(INTENTS)
        target[INTENTS.index(intent)] = 1.0
        items.append(
            {
                "ids": sequence,
                "markers": markers,
                "qtype": QTYPES["choice"],
                "target": target,
                "label": INTENTS.index(intent),
            }
        )
    return items


def collate(items: list[dict], pad_id: int) -> dict[str, torch.Tensor]:
    batch_size = len(items)
    max_len = max(len(item["ids"]) for item in items)
    max_options = max(len(item["markers"]) for item in items)
    ids = torch.full((batch_size, max_len), pad_id, dtype=torch.long)
    attention = torch.zeros((batch_size, max_len), dtype=torch.long)
    marker_pos = torch.zeros((batch_size, max_options), dtype=torch.long)
    marker_mask = torch.zeros((batch_size, max_options), dtype=torch.bool)
    target = torch.zeros((batch_size, max_options), dtype=torch.float32)
    for index, item in enumerate(items):
        length = len(item["ids"])
        ids[index, :length] = torch.tensor(item["ids"], dtype=torch.long)
        attention[index, :length] = 1
        count = len(item["markers"])
        marker_pos[index, :count] = torch.tensor(item["markers"], dtype=torch.long)
        marker_mask[index, :count] = True
        target[index, :count] = torch.tensor(item["target"], dtype=torch.float32)
    return {
        "input_ids": ids,
        "attention_mask": attention,
        "marker_pos": marker_pos,
        "marker_mask": marker_mask,
        "target": target,
        "qtype": torch.full((batch_size,), QTYPES["choice"], dtype=torch.long),
        "label": torch.tensor([item["label"] for item in items], dtype=torch.long),
    }


def move_batch(batch: dict[str, torch.Tensor], device: torch.device) -> dict[str, torch.Tensor]:
    return {key: value.to(device) for key, value in batch.items()}


def evaluate(
    model,
    items: list[dict],
    pad_id: int,
    batch_size: int,
    device: torch.device,
    *,
    use_amp: bool,
) -> dict:
    """Return top-1, macro-F1, per-intent recall and the confusion matrix."""

    model.eval()
    correct = 0
    confusion = [[0] * len(INTENTS) for _ in INTENTS]
    with torch.no_grad():
        for start in range(0, len(items), batch_size):
            batch = move_batch(collate(items[start : start + batch_size], pad_id), device)
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
            predictions = logits.argmax(-1)
            labels = batch["label"]
            correct += int((predictions == labels).sum().item())
            for label, prediction in zip(labels.tolist(), predictions.tolist()):
                confusion[label][prediction] += 1

    recalls: dict[str, float] = {}
    f1_scores: list[float] = []
    for index, name in enumerate(INTENTS):
        support = sum(confusion[index])
        predicted = sum(row[index] for row in confusion)
        recall = (confusion[index][index] / support) if support else 0.0
        precision = (confusion[index][index] / predicted) if predicted else 0.0
        recalls[name] = recall
        f1_scores.append(
            (2 * precision * recall / (precision + recall))
            if (precision + recall)
            else 0.0
        )
    macro_f1 = sum(f1_scores) / len(INTENTS)
    return {
        "top1": correct / max(1, len(items)),
        "macro_f1": macro_f1,
        "per_intent_recall": {name: round(value, 4) for name, value in recalls.items()},
        "confusion_matrix": {
            actual: {
                predicted: confusion[i][j]
                for j, predicted in enumerate(INTENTS)
                if confusion[i][j]
            }
            for i, actual in enumerate(INTENTS)
            if sum(confusion[i])
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        action="append",
        required=True,
        help="JSONL training dataset; repeat to combine multiple files",
    )
    parser.add_argument(
        "--test-dataset",
        type=Path,
        action="append",
        default=[],
        help="frozen JSONL evaluation set checked for text leakage",
    )
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=1)
    parser.add_argument("--lr-encoder", type=float, default=2e-5)
    parser.add_argument("--lr-head", type=float, default=1e-4)
    parser.add_argument("--train-encoder", action="store_true")
    parser.add_argument(
        "--freeze-encoder-epochs",
        type=int,
        default=1,
        help="epochs trained with the encoder frozen before unfreezing it",
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-len", type=int, default=384)
    parser.add_argument("--head-max-len", type=int, default=256)
    parser.add_argument("--holdout-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument(
        "--early-stop-patience",
        type=int,
        default=2,
        help="stop after this many epochs without a macro-F1 improvement",
    )
    args = parser.parse_args()

    if args.output_dir.name == "laya-intent-v5":
        raise RuntimeError("refusing to overwrite the v5 checkpoint")

    torch.set_num_threads(max(1, min(os.cpu_count() or 1, 24)))
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    use_amp = device.type == "cuda"

    model_dir = args.model_dir
    if model_dir is None:
        model_dir = Path(snapshot_download(MODEL_REPO, revision=MODEL_REVISION))
    _fix_tokenizer_config(str(model_dir))
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir / "tokenizer"))
    cfg = json.loads((model_dir / "rl_agent_config.json").read_text(encoding="utf-8"))
    cfg["max_len"] = args.max_len
    cfg["head_max_len"] = args.head_max_len
    cfg["gradient_checkpointing"] = False

    rows = [row for dataset_path in args.dataset for row in load_rows(dataset_path)]
    groups: list[tuple[str, list[dict]]] = [("training data", rows)]
    for path in args.test_dataset:
        groups.append((f"test set {path.name}", load_rows(path)))
    assert_no_duplicates(*groups)

    counts: dict[str, int] = {name: 0 for name in INTENTS}
    for row in rows:
        counts[row["intent"]] += 1
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "option_order": list(INTENTS),
                "rows": len(rows),
                "counts": counts,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    train_rows, validation_rows = stratified_split(
        rows,
        max(0.0, min(0.5, args.holdout_ratio)),
        args.seed,
    )
    train_items = build_items(train_rows, tokenizer, cfg)
    validation_items = build_items(validation_rows, tokenizer, cfg)
    if not train_items or not validation_items:
        raise RuntimeError("training and validation rows are both required")

    model = build_model(cfg, encoder_dir=str(model_dir / "encoder"))
    model.load_state_dict(load_file(str(model_dir / "model.safetensors")), strict=True)
    if args.train_encoder:
        model.encoder.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
        model.head_checkpointing = True
    else:
        for parameter in model.encoder.parameters():
            parameter.requires_grad = False
    model.to(device)
    model.train()

    encoder_parameters = [
        parameter
        for name, parameter in model.named_parameters()
        if name.startswith("encoder.")
    ]
    head_parameters = [
        parameter
        for name, parameter in model.named_parameters()
        if not name.startswith("encoder.")
    ]
    parameter_groups = [{"params": head_parameters, "lr": args.lr_head}]
    if args.train_encoder:
        parameter_groups.insert(0, {"params": encoder_parameters, "lr": args.lr_encoder})
    optimizer = torch.optim.AdamW(parameter_groups, weight_decay=0.01)
    trainable = [*encoder_parameters, *head_parameters]

    best_state: dict[str, torch.Tensor] | None = None
    best_metrics: dict | None = None
    best_epoch = 0
    freeze_epochs = max(0, args.freeze_encoder_epochs) if args.train_encoder else args.epochs
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def save_checkpoint(state: dict[str, torch.Tensor], metrics: dict, epoch: int) -> None:
        """Persist the best checkpoint so far; a long run stays recoverable."""

        save_file(state, str(args.output_dir / "model.safetensors"))
        if not (args.output_dir / "encoder").exists():
            shutil.copytree(
                model_dir / "encoder", args.output_dir / "encoder", dirs_exist_ok=True
            )
        if not (args.output_dir / "tokenizer").exists():
            shutil.copytree(
                model_dir / "tokenizer", args.output_dir / "tokenizer", dirs_exist_ok=True
            )
        output_cfg = json.loads(
            (model_dir / "rl_agent_config.json").read_text(encoding="utf-8")
        )
        output_cfg.update(
            {
                "fine_tuned": True,
                "model_name": "laya-info-agent-intents",
                "temperature": [1.0, 1.0, 1.0],
                "temperature_by_options": {},
                "max_len": args.max_len,
                "head_max_len": args.head_max_len,
                "intent_schema_version": SCHEMA_VERSION,
                "option_order": list(INTENTS),
                "training": {
                    "base_checkpoint": str(model_dir),
                    "epochs": args.epochs,
                    "freeze_encoder_epochs": freeze_epochs,
                    "batch_size": args.batch_size,
                    "grad_accum": args.grad_accum,
                    "encoder_lr": args.lr_encoder if args.train_encoder else None,
                    "head_lr": args.lr_head,
                    "seed": args.seed,
                    "rows": len(rows),
                    "counts": counts,
                    "best_epoch": epoch,
                    "hours": round((time.time() - started) / 3600, 2),
                },
                "validation_metrics": metrics,
            }
        )
        (args.output_dir / "rl_agent_config.json").write_text(
            json.dumps(output_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    started = time.time()
    stale_epochs = 0
    for epoch in range(1, args.epochs + 1):
        encoder_frozen = epoch <= freeze_epochs
        for parameter in encoder_parameters:
            parameter.requires_grad = not encoder_frozen
        if encoder_frozen:
            model.encoder.eval()
        else:
            model.encoder.train()
        random.shuffle(train_items)
        total_loss = 0.0
        batches = 0
        for start in range(0, len(train_items), args.batch_size):
            batch = move_batch(
                collate(
                    train_items[start : start + args.batch_size],
                    tokenizer.pad_token_id,
                ),
                device,
            )
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
                    detach_encoder=encoder_frozen,
                )
                mask = batch["marker_mask"]
                log_probs = torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)
                loss = -(batch["target"] * log_probs).sum(-1).mean()
                loss = loss / max(1, args.grad_accum)
            loss.backward()
            if (batches + 1) % max(1, args.grad_accum) == 0:
                torch.nn.utils.clip_grad_norm_(trainable, 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
            total_loss += float(loss.detach())
            batches += 1
        if batches % max(1, args.grad_accum) != 0:
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)

        metrics = evaluate(
            model,
            validation_items,
            tokenizer.pad_token_id,
            max(1, args.batch_size),
            device,
            use_amp=use_amp,
        )
        improved = best_metrics is None or metrics["macro_f1"] > best_metrics["macro_f1"]
        if improved:
            stale_epochs = 0
            best_metrics = metrics
            best_epoch = epoch
            best_state = {
                key: value.detach().half().contiguous().cpu().clone()
                for key, value in model.state_dict().items()
            }
            save_checkpoint(best_state, best_metrics, best_epoch)
        else:
            stale_epochs += 1
        print(
            json.dumps(
                {
                    "epoch": epoch,
                    "encoder_frozen": encoder_frozen,
                    "loss": round(total_loss / max(1, batches), 4),
                    "validation_top1": round(metrics["top1"], 4),
                    "validation_macro_f1": round(metrics["macro_f1"], 4),
                    "best_epoch": best_epoch,
                    "stale_epochs": stale_epochs,
                    "elapsed_seconds": round(time.time() - started, 1),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.early_stop_patience > 0 and stale_epochs >= args.early_stop_patience:
            print(
                json.dumps(
                    {"early_stop": True, "epoch": epoch, "best_epoch": best_epoch},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            break

    assert best_state is not None and best_metrics is not None
    save_checkpoint(best_state, best_metrics, best_epoch)
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "best_epoch": best_epoch,
                "validation_top1": round(best_metrics["top1"], 4),
                "validation_macro_f1": round(best_metrics["macro_f1"], 4),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
