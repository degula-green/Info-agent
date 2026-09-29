"""Fine-tune the Laya decision head for the Agent's nine intent labels."""

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

from laya.agent import _fix_tokenizer_config
from laya.common import QTYPES, build_model, build_sequence

MODEL_REPO = "convaiinnovations/laya-multilingual"
MODEL_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"
INSTRUCTION = "Choose the workflow that best matches the user's primary intent."
INTENTS = (
    "todo.create",
    "knowledge.answer",
    "web.research",
    "document.compare",
    "compliance.assess",
    "form.prepare",
    "form.submit",
    "non_task",
    "other_task",
)
CRITERIA = {
    "todo.create": (
        "Create a personal to-do, meeting, invitation, reminder, or future "
        "action item. This includes statements like I will do something tomorrow."
    ),
    "knowledge.answer": (
        "Answer a question from existing company or internal knowledge."
    ),
    "web.research": (
        "Search the public internet or external sources for information."
    ),
    "document.compare": (
        "Compare two or more documents and report their differences."
    ),
    "compliance.assess": (
        "Assess whether a person, document, or action complies with a rule or "
        "agreement."
    ),
    "form.prepare": (
        "Read a form and prepare a draft or preview before submission."
    ),
    "form.submit": "Submit an already prepared and confirmed form.",
    "non_task": (
        "Chit-chat, greetings, opinions, complaints, examples, hypotheses, "
        "completed past actions, or no clear goal."
    ),
    "other_task": (
        "A clear task that does not fit any category above, such as booking a "
        "train ticket."
    ),
}


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


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
        if intent not in INTENTS:
            continue
        sequence, markers = build_sequence(
            tokenizer,
            {"text": str(row["text"])},
            question,
            cfg["max_len"],
            cfg["head_max_len"],
        )
        if len(markers) != len(INTENTS):
            continue
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
) -> float:
    model.eval()
    correct = 0
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
            correct += int((predictions == batch["label"]).sum().item())
    return correct / max(1, len(items))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        action="append",
        required=True,
        help="JSONL dataset; repeat to combine multiple files",
    )
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=1)
    parser.add_argument("--lr-encoder", type=float, default=2e-5)
    parser.add_argument("--lr-head", type=float, default=1e-4)
    parser.add_argument("--train-encoder", action="store_true")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-len", type=int, default=512)
    parser.add_argument("--head-max-len", type=int, default=256)
    parser.add_argument("--holdout-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260929)
    args = parser.parse_args()

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

    rows = [
        row
        for dataset_path in args.dataset
        for row in load_rows(dataset_path)
    ]
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
    model.load_state_dict(
        load_file(str(model_dir / "model.safetensors")),
        strict=True,
    )
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
    if not args.train_encoder:
        model.encoder.eval()

    encoder_parameters = [
        parameter
        for name, parameter in model.named_parameters()
        if name.startswith("encoder.") and parameter.requires_grad
    ]
    head_parameters = [
        parameter
        for name, parameter in model.named_parameters()
        if not name.startswith("encoder.") and parameter.requires_grad
    ]
    parameter_groups = [
        {"params": head_parameters, "lr": args.lr_head},
    ]
    if encoder_parameters:
        parameter_groups.insert(
            0,
            {"params": encoder_parameters, "lr": args.lr_encoder},
        )
    optimizer = torch.optim.AdamW(parameter_groups, weight_decay=0.01)
    trainable = [*encoder_parameters, *head_parameters]

    started = time.time()
    for epoch in range(1, args.epochs + 1):
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
                    detach_encoder=not args.train_encoder,
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
        accuracy = evaluate(
            model,
            validation_items,
            tokenizer.pad_token_id,
            max(1, args.batch_size),
            device,
            use_amp=use_amp,
        )
        print(
            json.dumps(
                {
                    "epoch": epoch,
                    "loss": round(total_loss / max(1, batches), 4),
                    "validation_accuracy": round(accuracy, 4),
                    "elapsed_seconds": round(time.time() - started, 1),
                }
            ),
            flush=True,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    state = {
        key: value.half().contiguous().cpu()
        for key, value in model.state_dict().items()
    }
    save_file(state, str(args.output_dir / "model.safetensors"))
    shutil.copy2(model_dir / "rl_agent_config.json", args.output_dir / "rl_agent_config.json")
    shutil.copytree(model_dir / "encoder", args.output_dir / "encoder", dirs_exist_ok=True)
    shutil.copytree(model_dir / "tokenizer", args.output_dir / "tokenizer", dirs_exist_ok=True)
    output_cfg = json.loads((args.output_dir / "rl_agent_config.json").read_text(encoding="utf-8"))
    output_cfg.update(
        {
            "fine_tuned": True,
            "model_name": "laya-info-agent-intents",
            "temperature": [1.0, 1.0, 1.0],
            "temperature_by_options": {},
            "max_len": args.max_len,
            "head_max_len": args.head_max_len,
        }
    )
    (args.output_dir / "rl_agent_config.json").write_text(
        json.dumps(output_cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(args.output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
